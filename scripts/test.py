#!/usr/bin/env python3
#
# Script to compile and runs tests.
#

import collections as co
import errno
import glob
import itertools as it
import math as m
import os
import pty
import re
import shlex
import shutil
import signal
import subprocess as sp
import threading as th
import time
import toml


TEST_PATHS = ['tests']
RUNNER_PATH = './runners/test_runner'
HEADER_PATH = 'runners/test_runner.h'


def testpath(path):
    path, *_ = path.split('#', 1)
    return path

def testsuite(path):
    suite = testpath(path)
    suite = os.path.basename(suite)
    if suite.endswith('.toml'):
        suite = suite[:-len('.toml')]
    return suite

def testcase(path):
    _, case, *_ = path.split('#', 2)
    return '%s#%s' % (testsuite(path), case)

def openio(path, mode='r', buffering=-1, nb=False):
    if path == '-':
        if 'r' in mode:
            return os.fdopen(os.dup(sys.stdin.fileno()), 'r', buffering)
        else:
            return os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering)
    elif nb and 'a' in mode:
        return os.fdopen(os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK,
                0o666),
            mode,
            buffering)
    else:
        return open(path, mode, buffering)

def color(**args):
    if args.get('color') == 'auto':
        return sys.stdout.isatty()
    elif args.get('color') == 'always':
        return True
    else:
        return False

class TestCase:
    # create a TestCase object from a config
    def __init__(self, config, args={}):
        self.name = config.pop('name')
        self.path = config.pop('path')
        self.suite = config.pop('suite')
        self.lineno = config.pop('lineno', None)
        self.if_ = config.pop('if', None)
        if isinstance(self.if_, bool):
            self.if_ = 'true' if self.if_ else 'false'
        self.code = config.pop('code')
        self.code_lineno = config.pop('code_lineno', None)
        self.in_ = config.pop('in',
            config.pop('suite_in', None))

        self.reentrant = config.pop('reentrant',
            config.pop('suite_reentrant', False))

        # figure out defines and build possible permutations
        self.defines = set()
        self.permutations = []

        suite_defines = config.pop('suite_defines', {})
        if not isinstance(suite_defines, list):
            suite_defines = [suite_defines]
        defines = config.pop('defines', {})
        if not isinstance(defines, list):
            defines = [defines]

        # build possible permutations
        for suite_defines_ in suite_defines:
            self.defines |= suite_defines_.keys()
            for defines_ in defines:
                self.defines |= defines_.keys()
                self.permutations.extend(map(dict, it.product(*(
                    [(k, v) for v in (vs if isinstance(vs, list) else [vs])]
                    for k, vs in sorted(
                        (suite_defines_ | defines_).items())))))

        for k in config.keys():
            print('%swarning:%s in %s, found unused key %r' % (
                '\x1b[01;33m' if color(**args) else '',
                '\x1b[m' if color(**args) else '',
                self.id(),
                k),
                file=sys.stderr)

    def id(self):
        return '%s#%s' % (self.suite, self.name)


class TestSuite:
    # create a TestSuite object from a toml file
    def __init__(self, path, args={}):
        self.name = testsuite(path)
        self.path = testpath(path)

        # load toml file and parse test cases
        with open(self.path) as f:
            # load tests
            config = toml.load(f)

            # find line numbers
            f.seek(0)
            case_linenos = []
            code_linenos = []
            for i, line in enumerate(f):
                match = re.match(
                    '(?P<case>\[\s*cases\s*\.\s*(?P<name>\w+)\s*\])'
                        '|' '(?P<code>code\s*=)',
                    line)
                if match and match.group('case'):
                    case_linenos.append((i+1, match.group('name')))
                elif match and match.group('code'):
                    code_linenos.append(i+2)

            # sort in case toml parsing did not retain order
            case_linenos.sort()

            cases = config.pop('cases')
            for (lineno, name), (nlineno, _) in it.zip_longest(
                    case_linenos, case_linenos[1:],
                    fillvalue=(float('inf'), None)):
                code_lineno = min(
                    (l for l in code_linenos if l >= lineno and l < nlineno),
                    default=None)
                cases[name]['lineno'] = lineno
                cases[name]['code_lineno'] = code_lineno

            self.if_ = config.pop('if', None)
            if isinstance(self.if_, bool):
                self.if_ = 'true' if self.if_ else 'false'

            self.code = config.pop('code', None)
            self.code_lineno = min(
                (l for l in code_linenos
                    if not case_linenos or l < case_linenos[0][0]),
                default=None)

            # a couple of these we just forward to all cases
            defines = config.pop('defines', {})
            in_ = config.pop('in', None)
            reentrant = config.pop('reentrant', False)

            self.cases = []
            for name, case in sorted(cases.items(),
                    key=lambda c: c[1].get('lineno')):
                self.cases.append(TestCase(config={
                    'name': name,
                    'path': path + (':%d' % case['lineno']
                        if 'lineno' in case else ''),
                    'suite': self.name,
                    'suite_defines': defines,
                    'suite_in': in_,
                    'suite_reentrant': reentrant,
                    **case},
                    args=args))

            # combine per-case defines
            self.defines = set.union(*(
                set(case.defines) for case in self.cases))

            # combine other per-case things
            self.reentrant = any(case.reentrant for case in self.cases)

        for k in config.keys():
            print('%swarning:%s in %s, found unused key %r' % (
                '\x1b[01;33m' if color(**args) else '',
                '\x1b[m' if color(**args) else '',
                self.id(),
                k),
                file=sys.stderr)

    def id(self):
        return self.name



def compile(**args):
    # find .toml files
    paths = []
    for path in args.get('test_ids', TEST_PATHS):
        if os.path.isdir(path):
            path = path + '/*.toml'

        for path in glob.glob(path):
            paths.append(path)

    if not paths:
        print('no test suites found in %r?' % args['test_ids'])
        sys.exit(-1)

    if not args.get('source'):
        if len(paths) > 1:
            print('more than one test suite for compilation? (%r)'
                % args['test_ids'])
            sys.exit(-1)

        # load our suite
        suite = TestSuite(paths[0], args)
    else:
        # load all suites
        suites = [TestSuite(path, args) for path in paths]
        suites.sort(key=lambda s: s.name)

    # write generated test source
    if 'output' in args:
        with openio(args['output'], 'w') as f:
            _write = f.write
            def write(s):
                f.lineno += s.count('\n')
                _write(s)
            def writeln(s=''):
                f.lineno += s.count('\n') + 1
                _write(s)
                _write('\n')
            f.lineno = 1
            f.write = write
            f.writeln = writeln

            f.writeln("// Generated by %s:" % sys.argv[0])
            f.writeln("//")
            f.writeln("// %s" % ' '.join(sys.argv))
            f.writeln("//")
            f.writeln()

            # include test_runner.h in every generated file
            f.writeln("#include \"%s\"" % HEADER_PATH)

            # write out generated functions, this can end up in different
            # files depending on the "in" attribute
            #
            # note it's up to the specific generated file to declare
            # the test defines
            def write_case_functions(f, suite, case):
                    # create case define functions
                    if case.defines:
                        # deduplicate defines by value to try to reduce the
                        # number of functions we generate
                        define_cbs = {}
                        for i, defines in enumerate(case.permutations):
                            for k, v in sorted(defines.items()):
                                if v not in define_cbs:
                                    name = ('__test__%s__%s__%s__%d'
                                        % (suite.name, case.name, k, i))
                                    define_cbs[v] = name
                                    f.writeln('intmax_t %s(void) {' % name)
                                    f.writeln(4*' '+'return %s;' % v)
                                    f.writeln('}')
                                    f.writeln()
                        f.writeln('intmax_t (*const *const '
                            '__test__%s__%s__defines[])(void) = {'
                            % (suite.name, case.name))
                        for defines in case.permutations:
                            f.writeln(4*' '+'(intmax_t (*const[])(void)){')
                            for define in sorted(suite.defines):
                                f.writeln(8*' '+'%s,' % (
                                    define_cbs[defines[define]]
                                        if define in defines
                                        else 'NULL'))
                            f.writeln(4*' '+'},')
                        f.writeln('};')
                        f.writeln()    

                    # create case filter function
                    if suite.if_ is not None or case.if_ is not None:
                        f.writeln('bool __test__%s__%s__filter(void) {'
                            % (suite.name, case.name))
                        f.writeln(4*' '+'return %s;'
                            % ' && '.join('(%s)' % if_
                                for if_ in [suite.if_, case.if_]
                                if if_ is not None))
                        f.writeln('}')
                        f.writeln()

                    # create case run function
                    f.writeln('void __test__%s__%s__run('
                        '__attribute__((unused)) struct lfs_config *cfg) {'
                        % (suite.name, case.name))
                    f.writeln(4*' '+'// test case %s' % case.id())
                    if case.code_lineno is not None:
                        f.writeln(4*' '+'#line %d "%s"'
                            % (case.code_lineno, suite.path))
                    f.write(case.code)
                    if case.code_lineno is not None:
                        f.writeln(4*' '+'#line %d "%s"'
                            % (f.lineno+1, args['output']))
                    f.writeln('}')
                    f.writeln()

            if not args.get('source'):
                if suite.code is not None:
                    if suite.code_lineno is not None:
                        f.writeln('#line %d "%s"'
                            % (suite.code_lineno, suite.path))
                    f.write(suite.code)
                    if suite.code_lineno is not None:
                        f.writeln('#line %d "%s"'
                            % (f.lineno+1, args['output']))
                    f.writeln()

                if suite.defines:
                    for i, define in enumerate(sorted(suite.defines)):
                        f.writeln('#ifndef %s' % define)
                        f.writeln('#define %-24s test_define(%d)'
                            % (define, i))
                        f.writeln('#endif')
                    f.writeln()

                # create case functions
                for case in suite.cases:
                    if case.in_ is None:
                        write_case_functions(f, suite, case)
                    else:
                        if case.defines:
                            f.writeln('extern intmax_t (*const *const '
                                '__test__%s__%s__defines[])(void);'
                                % (suite.name, case.name))
                        if suite.if_ is not None or case.if_ is not None:
                            f.writeln('extern bool __test__%s__%s__filter('
                                'void);'
                                % (suite.name, case.name))
                        f.writeln('extern void __test__%s__%s__run('
                            'struct lfs_config *cfg);'
                            % (suite.name, case.name))
                        f.writeln()

                # create suite struct
                f.writeln('__attribute__((section("_test_suites")))')
                f.writeln('const struct test_suite __test__%s__suite = {'
                    % suite.name)
                f.writeln(4*' '+'.id = "%s",' % suite.id())
                f.writeln(4*' '+'.name = "%s",' % suite.name)
                f.writeln(4*' '+'.path = "%s",' % suite.path)
                f.writeln(4*' '+'.flags = %s,'
                    % (' | '.join(filter(None, [
                        'TEST_REENTRANT' if suite.reentrant else None]))
                        or 0))
                if suite.defines:
                    # create suite define names
                    f.writeln(4*' '+'.define_names = (const char *const[]){')
                    for k in sorted(suite.defines):
                        f.writeln(8*' '+'"%s",' % k)
                    f.writeln(4*' '+'},')
                f.writeln(4*' '+'.define_count = %d,' % len(suite.defines))
                f.writeln(4*' '+'.cases = (const struct test_case[]){')
                for case in suite.cases:
                    # create case structs
                    f.writeln(8*' '+'{')
                    f.writeln(12*' '+'.id = "%s",' % case.id())
                    f.writeln(12*' '+'.name = "%s",' % case.name)
                    f.writeln(12*' '+'.path = "%s",' % case.path)
                    f.writeln(12*' '+'.flags = %s,'
                        % (' | '.join(filter(None, [
                            'TEST_REENTRANT' if case.reentrant else None]))
                            or 0))
                    f.writeln(12*' '+'.permutations = %d,'
                        % len(case.permutations))
                    if case.defines:
                        f.writeln(12*' '+'.defines = __test__%s__%s__defines,'
                            % (suite.name, case.name))
                    if suite.if_ is not None or case.if_ is not None:
                        f.writeln(12*' '+'.filter = __test__%s__%s__filter,'
                            % (suite.name, case.name))
                    f.writeln(12*' '+'.run = __test__%s__%s__run,'
                        % (suite.name, case.name))
                    f.writeln(8*' '+'},')
                f.writeln(4*' '+'},')
                f.writeln(4*' '+'.case_count = %d,' % len(suite.cases))
                f.writeln('};')
                f.writeln()

            else:
                # copy source
                f.writeln('#line 1 "%s"' % args['source'])
                with open(args['source']) as sf:
                    shutil.copyfileobj(sf, f)
                f.writeln()

                # write any internal tests
                for suite in suites:
                    for case in suite.cases:
                        if (case.in_ is not None
                                and os.path.normpath(case.in_)
                                    == os.path.normpath(args['source'])):
                            # write defines, but note we need to undef any
                            # new defines since we're in someone else's file
                            if suite.defines:
                                for i, define in enumerate(
                                        sorted(suite.defines)):
                                    f.writeln('#ifndef %s' % define)
                                    f.writeln('#define %-24s test_define(%d)'
                                        % (define, i))
                                    f.writeln('#define __TEST__%s__NEEDS_UNDEF'
                                        % define)
                                    f.writeln('#endif')
                                f.writeln()

                            write_case_functions(f, suite, case)

                            if suite.defines:
                                for define in sorted(suite.defines):
                                    f.writeln('#ifdef __TEST__%s__NEEDS_UNDEF'
                                        % define)
                                    f.writeln('#undef __TEST__%s__NEEDS_UNDEF'
                                        % define)
                                    f.writeln('#undef %s' % define)
                                    f.writeln('#endif')
                                f.writeln()

def runner(**args):
    cmd = args['runner'].copy()
    cmd.extend(args.get('test_ids'))

    # run under some external command?
    cmd[:0] = args.get('exec', [])

    # run under valgrind?
    if args.get('valgrind'):
        cmd[:0] = filter(None, [
            'valgrind',
            '--leak-check=full',
            '--track-origins=yes',
            '--error-exitcode=4',
            '-q'])

    # other context
    if args.get('geometry'):
        cmd.append('-G%s' % args.get('geometry'))

    if args.get('powerloss'):
        cmd.append('-p%s' % args.get('powerloss'))

    # defines?
    if args.get('define'):
        for define in args.get('define'):
            cmd.append('-D%s' % define)

    return cmd

def list_(**args):
    cmd = runner(**args)
    if args.get('summary'):          cmd.append('--summary')
    if args.get('list_suites'):      cmd.append('--list-suites')
    if args.get('list_cases'):       cmd.append('--list-cases')
    if args.get('list_paths'):       cmd.append('--list-paths')
    if args.get('list_defines'):     cmd.append('--list-defines')
    if args.get('list_defaults'):    cmd.append('--list-defaults')
    if args.get('list_geometries'):  cmd.append('--list-geometries')
    if args.get('list_powerlosses'): cmd.append('--list-powerlosses')

    if args.get('verbose'):
        print(' '.join(shlex.quote(c) for c in cmd))
    sys.exit(sp.call(cmd))


def find_cases(runner_, **args):
    # query from runner
    cmd = runner_ + ['--list-cases']
    if args.get('verbose'):
        print(' '.join(shlex.quote(c) for c in cmd))
    proc = sp.Popen(cmd,
        stdout=sp.PIPE,
        stderr=sp.PIPE if not args.get('verbose') else None,
        universal_newlines=True,
        errors='replace',
        close_fds=False)
    expected_suite_perms = co.defaultdict(lambda: 0)
    expected_case_perms = co.defaultdict(lambda: 0)
    expected_perms = 0
    total_perms = 0
    pattern = re.compile(
        '^(?P<id>(?P<case>(?P<suite>[^#]+)#[^\s#]+)[^\s]*)\s+'
            '[^\s]+\s+(?P<filtered>\d+)/(?P<perms>\d+)')
    # skip the first line
    for line in it.islice(proc.stdout, 1, None):
        m = pattern.match(line)
        if m:
            filtered = int(m.group('filtered'))
            expected_suite_perms[m.group('suite')] += filtered
            expected_case_perms[m.group('id')] += filtered
            expected_perms += filtered
            total_perms += int(m.group('perms'))
    proc.wait()
    if proc.returncode != 0:
        if not args.get('verbose'):
            for line in proc.stderr:
                sys.stdout.write(line)
        sys.exit(-1)

    return (
        expected_suite_perms,
        expected_case_perms,
        expected_perms,
        total_perms)

def find_paths(runner_, **args):
    # query from runner
    cmd = runner_ + ['--list-paths']
    if args.get('verbose'):
        print(' '.join(shlex.quote(c) for c in cmd))
    proc = sp.Popen(cmd,
        stdout=sp.PIPE,
        stderr=sp.PIPE if not args.get('verbose') else None,
        universal_newlines=True,
        errors='replace',
        close_fds=False)
    paths = co.OrderedDict()
    pattern = re.compile(
        '^(?P<id>(?P<case>(?P<suite>[^#]+)#[^\s#]+)[^\s]*)\s+'
            '(?P<path>[^:]+):(?P<lineno>\d+)')
    for line in proc.stdout:
        m = pattern.match(line)
        if m:
            paths[m.group('id')] = (m.group('path'), int(m.group('lineno')))
    proc.wait()
    if proc.returncode != 0:
        if not args.get('verbose'):
            for line in proc.stderr:
                sys.stdout.write(line)
        sys.exit(-1)

    return paths

def find_defines(runner_, **args):
    # query from runner
    cmd = runner_ + ['--list-defines']
    if args.get('verbose'):
        print(' '.join(shlex.quote(c) for c in cmd))
    proc = sp.Popen(cmd,
        stdout=sp.PIPE,
        stderr=sp.PIPE if not args.get('verbose') else None,
        universal_newlines=True,
        errors='replace',
        close_fds=False)
    defines = co.OrderedDict()
    pattern = re.compile(
        '^(?P<id>(?P<case>(?P<suite>[^#]+)#[^\s#]+)[^\s]*)\s+'
            '(?P<defines>(?:\w+=\w+\s*)+)')
    for line in proc.stdout:
        m = pattern.match(line)
        if m:
            defines[m.group('id')] = {k: v
                for k, v in re.findall('(\w+)=(\w+)', m.group('defines'))}
    proc.wait()
    if proc.returncode != 0:
        if not args.get('verbose'):
            for line in proc.stderr:
                sys.stdout.write(line)
        sys.exit(-1)

    return defines


class TestFailure(Exception):
    def __init__(self, id, returncode, output, assert_=None):
        self.id = id
        self.returncode = returncode
        self.output = output
        self.assert_ = assert_

def run_stage(name, runner_, **args):
    # get expected suite/case/perm counts
    expected_suite_perms, expected_case_perms, expected_perms, total_perms = (
        find_cases(runner_, **args))

    passed_suite_perms = co.defaultdict(lambda: 0)
    passed_case_perms = co.defaultdict(lambda: 0)
    passed_perms = 0
    powerlosses = 0
    failures = []
    killed = False

    pattern = re.compile('^(?:'
            '(?P<op>running|finished|skipped|powerloss) '
                '(?P<id>(?P<case>(?P<suite>[^#]+)#[^\s#]+)[^\s]*)'
            '|' '(?P<path>[^:]+):(?P<lineno>\d+):(?P<op_>assert):'
                ' *(?P<message>.*)' ')$')
    locals = th.local()
    children = set()

    def run_runner(runner_):
        nonlocal passed_suite_perms
        nonlocal passed_case_perms
        nonlocal passed_perms
        nonlocal powerlosses
        nonlocal locals

        # run the tests!
        cmd = runner_.copy()
        # TODO move all these to runner?
        if args.get('disk'):
            cmd.append('--disk=%s' % args['disk'])
        if args.get('trace'):
            cmd.append('--trace=%s' % args['trace'])
        if args.get('read_sleep'):
            cmd.append('--read-sleep=%s' % args['read_sleep'])
        if args.get('prog_sleep'):
            cmd.append('--prog-sleep=%s' % args['prog_sleep'])
        if args.get('erase_sleep'):
            cmd.append('--erase-sleep=%s' % args['erase_sleep'])
        if args.get('verbose'):
            print(' '.join(shlex.quote(c) for c in cmd))

        mpty, spty = pty.openpty()
        proc = sp.Popen(cmd, stdout=spty, stderr=spty, close_fds=False)
        os.close(spty)
        children.add(proc)
        mpty = os.fdopen(mpty, 'r', 1)
        output = None

        last_id = None
        last_output = []
        last_assert = None
        try:
            while True:
                # parse a line for state changes
                try:
                    line = mpty.readline()
                except OSError as e:
                    if e.errno == errno.EIO:
                        break
                    raise
                if not line:
                    break
                last_output.append(line)
                if args.get('output'):
                    try:
                        if not output:
                            output = openio(args['output'], 'a', 1, nb=True)
                        output.write(line)
                    except OSError as e:
                        if e.errno not in [
                                errno.ENXIO,
                                errno.EPIPE,
                                errno.EAGAIN]:
                            raise
                        output = None
                if args.get('verbose'):
                    sys.stdout.write(line)

                m = pattern.match(line)
                if m:
                    op = m.group('op') or m.group('op_')
                    if op == 'running':
                        locals.seen_perms += 1
                        last_id = m.group('id')
                        last_output = []
                        last_assert = None
                    elif op == 'powerloss':
                        last_id = m.group('id')
                        powerlosses += 1
                    elif op == 'finished':
                        passed_suite_perms[m.group('suite')] += 1
                        passed_case_perms[m.group('case')] += 1
                        passed_perms += 1
                    elif op == 'skipped':
                        locals.seen_perms += 1
                    elif op == 'assert':
                        last_assert = (
                            m.group('path'),
                            int(m.group('lineno')),
                            m.group('message'))
                        # go ahead and kill the process, aborting takes a while
                        if args.get('keep_going'):
                            proc.kill()
        except KeyboardInterrupt:
            raise TestFailure(last_id, 1, last_output)
        finally:
            children.remove(proc)
            mpty.close()

        proc.wait()
        if proc.returncode != 0:
            raise TestFailure(
                last_id,
                proc.returncode,
                last_output,
                last_assert)

    def run_job(runner, start=None, step=None):
        nonlocal failures
        nonlocal killed
        nonlocal locals

        start = start or 0
        step = step or 1
        while start < total_perms:
            runner_ = runner.copy()
            if start is not None:
                runner_.append('--start=%d' % start)
            if step is not None:
                runner_.append('--step=%d' % step)
            if args.get('isolate') or args.get('valgrind'):
                runner_.append('--stop=%d' % (start+step))

            try:
                # run the tests
                locals.seen_perms = 0
                run_runner(runner_)
                assert locals.seen_perms > 0
                start += locals.seen_perms*step

            except TestFailure as failure:
                # race condition for multiple failures?
                if failures and not args.get('keep_going'):
                    break

                failures.append(failure)

                if args.get('keep_going') and not killed:
                    # resume after failed test
                    assert locals.seen_perms > 0
                    start += locals.seen_perms*step
                    continue
                else:
                    # stop other tests
                    killed = True
                    for child in children.copy():
                        child.kill()
                    break
    

    # parallel jobs?
    runners = []
    if 'jobs' in args:
        for job in range(args['jobs']):
            runners.append(th.Thread(
                target=run_job, args=(runner_, job, args['jobs']),
                daemon=True))
    else:
        runners.append(th.Thread(
            target=run_job, args=(runner_, None, None),
            daemon=True))

    def print_update(done):
        if not args.get('verbose') and (color(**args) or done):
            sys.stdout.write('%s%srunning %s%s:%s %s%s' % (
                '\r\x1b[K' if color(**args) else '',
                '\x1b[?7l' if not done else '',
                ('\x1b[32m' if not failures else '\x1b[31m')
                    if color(**args) else '',
                name,
                '\x1b[m' if color(**args) else '',
                ', '.join(filter(None, [
                    '%d/%d suites' % (
                        sum(passed_suite_perms[k] == v
                            for k, v in expected_suite_perms.items()),
                        len(expected_suite_perms))
                        if (not args.get('by_suites')
                            and not args.get('by_cases')) else None,
                    '%d/%d cases' % (
                        sum(passed_case_perms[k] == v
                            for k, v in expected_case_perms.items()),
                        len(expected_case_perms))
                        if not args.get('by_cases') else None,
                    '%d/%d perms' % (passed_perms, expected_perms),
                    '%dpls!' % powerlosses
                        if powerlosses else None,
                    '%s%d/%d failures%s' % (
                            '\x1b[31m' if color(**args) else '',
                            len(failures),
                            expected_perms,
                            '\x1b[m' if color(**args) else '')
                        if failures else None])),
                '\x1b[?7h' if not done else '\n'))
            sys.stdout.flush()

    for r in runners:
        r.start()

    try:
        while any(r.is_alive() for r in runners):
            time.sleep(0.01)
            print_update(False)
    except KeyboardInterrupt:
        # this is handled by the runner threads, we just
        # need to not abort here
        killed = True
    finally:
        print_update(True)

    for r in runners:
        r.join()

    return (
        expected_perms,
        passed_perms,
        powerlosses,
        failures,
        killed)
    

def run(**args):
    # query runner for tests
    runner_ = runner(**args)
    print('using runner: %s'
        % ' '.join(shlex.quote(c) for c in runner_))
    expected_suite_perms, expected_case_perms, expected_perms, total_perms = (
        find_cases(runner_, **args))
    print('found %d suites, %d cases, %d/%d permutations'
        % (len(expected_suite_perms),
            len(expected_case_perms),
            expected_perms,
            total_perms))
    print()

    # truncate and open logs here so they aren't disconnected between tests
    output = None
    if args.get('output'):
        output = openio(args['output'], 'w', 1)
    trace = None
    if args.get('trace'):
        trace = openio(args['trace'], 'w', 1)

    # measure runtime
    start = time.time()

    # spawn runners
    expected = 0
    passed = 0
    powerlosses = 0
    failures = []
    for by in (expected_case_perms.keys() if args.get('by_cases')
            else expected_suite_perms.keys() if args.get('by_suites')
            else [None]):
        # rebuild runner for each stage to override test identifier if needed
        stage_runner = runner(**args | {
            'test_ids': [by] if by is not None else args.get('test_ids', [])})

        # spawn jobs for stage
        expected_, passed_, powerlosses_, failures_, killed = run_stage(
            by or 'tests',
            stage_runner,
            **args)
        expected += expected_
        passed += passed_
        powerlosses += powerlosses_
        failures.extend(failures_)
        if (failures and not args.get('keep_going')) or killed:
            break

    stop = time.time()

    if output:
        output.close()
    if trace:
        trace.close()

    # show summary
    print()
    print('%sdone:%s %s' % (
        ('\x1b[32m' if not failures else '\x1b[31m')
            if color(**args) else '',
        '\x1b[m' if color(**args) else '',
        ', '.join(filter(None, [
            '%d/%d passed' % (passed, expected),
            '%d/%d failed' % (len(failures), expected),
            '%dpls!' % powerlosses if powerlosses else None,
            'in %.2fs' % (stop-start)]))))
    print()

    # print each failure
    if failures:
        # get some extra info from runner
        runner_paths = find_paths(runner_, **args)
        runner_defines = find_defines(runner_, **args)

    for failure in failures:
        # show summary of failure
        path, lineno = runner_paths[testcase(failure.id)]
        defines = runner_defines.get(failure.id, {})

        print('%s%s:%d:%sfailure:%s %s%s failed' % (
            '\x1b[01m' if color(**args) else '',
            path, lineno,
            '\x1b[01;31m' if color(**args) else '',
            '\x1b[m' if color(**args) else '',
            failure.id,
            ' (%s)' % ', '.join('%s=%s' % (k,v) for k,v in defines.items())
                if defines else ''))

        if failure.output:
            output = failure.output
            if failure.assert_ is not None:
                output = output[:-1]
            for line in output[-5:]:
                sys.stdout.write(line)

        if failure.assert_ is not None:
            path, lineno, message = failure.assert_
            print('%s%s:%d:%sassert:%s %s' % (
                '\x1b[01m' if color(**args) else '',
                path, lineno,
                '\x1b[01;31m' if color(**args) else '',
                '\x1b[m' if color(**args) else '',
                message))
            with open(path) as f:
                line = next(it.islice(f, lineno-1, None)).strip('\n')
                print(line)
        print()

    # drop into gdb?
    if failures and (args.get('gdb')
            or args.get('gdb_case')
            or args.get('gdb_main')):
        failure = failures[0]
        runner_ = runner(**args | {'test_ids': [failure.id]})

        if args.get('gdb_main'):
            cmd = ['gdb',
                '-ex', 'break main',
                '-ex', 'run',
                '--args'] + runner_
        elif args.get('gdb_case'):
            path, lineno = runner_paths[testcase(failure.id)]
            cmd = ['gdb',
                '-ex', 'break %s:%d' % (path, lineno),
                '-ex', 'run',
                '--args'] + runner_
        elif failure.assert_ is not None:
            cmd = ['gdb',
                '-ex', 'run',
                '-ex', 'frame function raise',
                '-ex', 'up 2',
                '--args'] + runner_
        else:
            cmd = ['gdb',
                '-ex', 'run',
                '--args'] + runner_

        # exec gdb interactively
        if args.get('verbose'):
            print(' '.join(shlex.quote(c) for c in cmd))
        os.execvp(cmd[0], cmd)

    return 1 if failures else 0


def main(**args):
    if args.get('compile'):
        compile(**args)
    elif (args.get('summary')
            or args.get('list_suites')
            or args.get('list_cases')
            or args.get('list_paths')
            or args.get('list_defines')
            or args.get('list_defaults')
            or args.get('list_geometries')
            or args.get('list_powerlosses')):
        list_(**args)
    else:
        run(**args)


if __name__ == "__main__":
    import argparse
    import sys
    argparse.ArgumentParser._handle_conflict_ignore = lambda *_: None
    argparse._ArgumentGroup._handle_conflict_ignore = lambda *_: None
    parser = argparse.ArgumentParser(
        description="Build and run tests.",
        conflict_handler='ignore')
    parser.add_argument('test_ids', nargs='*',
        help="Description of testis to run. May be a directory, path, or \
            test identifier. Test identifiers are of the form \
            <suite_name>#<case_name>#<permutation>, but suffixes can be \
            dropped to run any matching tests. Defaults to %s." % TEST_PATHS)
    parser.add_argument('-v', '--verbose', action='store_true',
        help="Output commands that run behind the scenes.")
    parser.add_argument('--color',
        choices=['never', 'always', 'auto'], default='auto',
        help="When to use terminal colors.")
    # test flags
    test_parser = parser.add_argument_group('test options')
    test_parser.add_argument('-Y', '--summary', action='store_true',
        help="Show quick summary.")
    test_parser.add_argument('-l', '--list-suites', action='store_true',
        help="List test suites.")
    test_parser.add_argument('-L', '--list-cases', action='store_true',
        help="List test cases.")
    test_parser.add_argument('--list-paths', action='store_true',
        help="List the path for each test case.")
    test_parser.add_argument('--list-defines', action='store_true',
        help="List the defines for each test permutation.")
    test_parser.add_argument('--list-defaults', action='store_true',
        help="List the default defines in this test-runner.")
    test_parser.add_argument('--list-geometries', action='store_true',
        help="List the disk geometries used for testing.")
    test_parser.add_argument('--list-powerlosses', action='store_true',
        help="List the available power-loss scenarios.")
    test_parser.add_argument('-D', '--define', action='append',
        help="Override a test define.")
    test_parser.add_argument('-G', '--geometry',
        help="Filter by geometry.")
    test_parser.add_argument('-p', '--powerloss',
        help="Comma-separated list of power-loss scenarios to test. \
            Defaults to 0,l.")
    test_parser.add_argument('-d', '--disk',
        help="Direct block device operations to this file.")
    test_parser.add_argument('-t', '--trace',
        help="Direct trace output to this file.")
    test_parser.add_argument('-o', '--output',
        help="Direct stdout and stderr to this file.")
    test_parser.add_argument('--read-sleep',
        help="Artificial read delay in seconds.")
    test_parser.add_argument('--prog-sleep',
        help="Artificial prog delay in seconds.")
    test_parser.add_argument('--erase-sleep',
        help="Artificial erase delay in seconds.")
    test_parser.add_argument('--runner', default=[RUNNER_PATH],
        type=lambda x: x.split(),
        help="Path to runner, defaults to %r" % RUNNER_PATH)
    test_parser.add_argument('-j', '--jobs', nargs='?', type=int,
        const=len(os.sched_getaffinity(0)),
        help="Number of parallel runners to run.")
    test_parser.add_argument('-k', '--keep-going', action='store_true',
        help="Don't stop on first error.")
    test_parser.add_argument('-i', '--isolate', action='store_true',
        help="Run each test permutation in a separate process.")
    test_parser.add_argument('-b', '--by-suites', action='store_true',
        help="Step through tests by suite.")
    test_parser.add_argument('-B', '--by-cases', action='store_true',
        help="Step through tests by case.")
    test_parser.add_argument('--gdb', action='store_true',
        help="Drop into gdb on test failure.")
    test_parser.add_argument('--gdb-case', action='store_true',
        help="Drop into gdb on test failure but stop at the beginning \
            of the failing test case.")
    test_parser.add_argument('--gdb-main', action='store_true',
        help="Drop into gdb on test failure but stop at the beginning \
            of main.")
    test_parser.add_argument('--exec', default=[], type=lambda e: e.split(),
        help="Run under another executable.")
    test_parser.add_argument('--valgrind', action='store_true',
        help="Run under Valgrind to find memory errors. Implicitly sets \
            --isolate.")
    # compilation flags
    comp_parser = parser.add_argument_group('compilation options')
    comp_parser.add_argument('-c', '--compile', action='store_true',
        help="Compile a test suite or source file.")
    comp_parser.add_argument('-s', '--source',
        help="Source file to compile, possibly injecting internal tests.")
    comp_parser.add_argument('-o', '--output',
        help="Output file.")
    sys.exit(main(**{k: v
        for k, v in vars(parser.parse_args()).items()
        if v is not None}))
