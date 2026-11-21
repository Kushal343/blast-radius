"""Rerun small candidate sets with patched library modules loaded from memory."""

import contextlib
import importlib
import importlib.machinery
import json
import linecache
import os
import sys
import time
import unittest
from pathlib import Path

from replay_cache import InlineExecutor, checkpoint_key, digest


class UnmatchedCheckpoint(BaseException):
    """Leave this test selected as soon as the replay sees a novel plan."""


class PatchedSource(importlib.machinery.SourceFileLoader):
    def __init__(self, original, source):
        super().__init__(original.name, original.path)
        self.source = source

    def get_code(self, fullname):
        return self.source_to_code(self.source.encode('utf-8'), self.path)

    def get_source(self, fullname):
        return self.source


class Overlay(importlib.machinery.PathFinder):
    def __init__(self, root, replacements):
        self.files = {os.path.normcase(str(root / name)): text for name, text in replacements.items()}
        for name, text in self.files.items():
            linecache.cache[name] = (len(text), None, text.splitlines(keepends=True), name)

    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'sqlglot' and not fullname.startswith('sqlglot.'):
            return None
        spec = super().find_spec(fullname, path, target)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            source = self.files.get(os.path.normcase(spec.origin))
            if source is not None:
                spec.loader = PatchedSource(spec.loader, source)
        return spec


class Results(unittest.TestResult):
    def __init__(self, cases):
        super().__init__()
        self.passed = set()
        self.complete = True
        self.cases = cases
        self.current = None
        self.failfast = True

    def stop(self):
        # unittest still stops each failed subtest method, while the other
        # methods in this replay can run and report their own outcomes.
        pass

    def startTest(self, test):
        super().startTest(test)
        self.current = self.cases.get(test)

    def stopTest(self, test):
        self.current = None
        super().stopTest(test)

    def addSuccess(self, test):
        self.passed.add(test)

    def addError(self, test, error):
        if not isinstance(test, unittest.TestCase):
            self.complete = False
        super().addError(test, error)


def unit_tests(names):
    cases = {}
    for name in names:
        parts = name.split('::')
        if len(parts) != 3 or not parts[0].endswith('.py'):
            continue
        file, class_name, method = parts
        try:
            module = importlib.import_module(file.removesuffix('.py').replace('/', '.'))
            kind = getattr(module, class_name)
            function = getattr(kind, method)
            if (issubclass(kind, unittest.TestCase) and not hasattr(module, 'pytestmark')
                    and not hasattr(kind, 'pytestmark') and not hasattr(function, 'pytestmark')):
                cases[kind(method)] = name
        except (AttributeError, ImportError, TypeError):
            continue
    return cases


def use_checkpoints(checkpoints, results):
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import Table
    from tests import test_executor

    ordinary = PythonExecutor.execute
    active_index = None

    class IndexedReplay(InlineExecutor):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.next_index = 0

        def submit(self, task, *args, **kwargs):
            nonlocal active_index
            entries = checkpoints.get(results.current)
            if entries is None:
                return super().submit(task, *args, **kwargs)
            selected = entries.get('cases')
            active_index = self.next_index if selected is None else selected[self.next_index]
            self.next_index += 1
            try:
                return super().submit(task, *args, **kwargs)
            finally:
                active_index = None

    def cached(executor, plan, outer_scope=None):
        entries = checkpoints.get(results.current)
        if entries is None:
            return ordinary(executor, plan, outer_scope)
        expected = entries['plans'].get(str(active_index), ())
        if outer_scope is not None or checkpoint_key(executor, plan) not in expected:
            raise UnmatchedCheckpoint('changed executor inputs require the original test')
        return True

    def setup(cls):
        cls.tpch_tables = {table: Table(columns) for table, columns in test_executor.TPCH_SCHEMA.items()}
        cls.tpcds_tables = {table: Table(columns) for table, columns in test_executor.TPCDS_SCHEMA.items()}
        cls.tpch_sqls = list(test_executor.load_sql_fixture_pairs('optimizer/tpc-h/tpc-h.sql'))
        cls.tpcds_sqls = list(test_executor.load_sql_fixture_pairs('optimizer/tpc-ds/tpc-ds.sql'))
        for name, entries in checkpoints.items():
            if 'cases' not in entries:
                continue
            field = name.rsplit('test_execute_', 1)[1] + '_sqls'
            queries = getattr(cls, field)
            setattr(cls, field, [queries[index] for index in entries['cases']])

    def check(self, index, table, tpch=True):
        _, sql, _ = (self.tpch_sqls if tpch else self.tpcds_sqls)[index]
        reference = checkpoints[results.current]['reference_sql'].get(digest(sql))
        rendering = test_executor.transpile(sql, write='duckdb')[0]
        if table is not True or reference != digest(rendering):
            raise UnmatchedCheckpoint('changed reference SQL requires the original test')

    test_executor.ProcessPoolExecutor = IndexedReplay
    test_executor.TestExecutor.setUpClass = classmethod(setup)
    test_executor.TestExecutor.tearDownClass = classmethod(lambda cls: None)
    test_executor.TestExecutor.subtestHelper = check
    PythonExecutor.execute = cached


def main():
    started = time.process_time()
    root = Path(sys.argv[1])
    request = json.load(sys.stdin)
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.meta_path.insert(0, Overlay(root, request['files']))
    os.environ['SKIP_INTEGRATION'] = '0'

    with open(os.devnull, 'w', encoding='utf-8') as sink, contextlib.redirect_stdout(sink):
        cases = unit_tests(request['tests'])
        optimizer_tests = sys.modules.get('tests.test_optimizer')
        if optimizer_tests is not None:
            optimizer_tests.ProcessPoolExecutor = InlineExecutor
        results = Results(cases)
        if checkpoints := request.get('checkpoints'):
            use_checkpoints(checkpoints, results)
        try:
            unittest.TestSuite(cases).run(results)
        except UnmatchedCheckpoint:
            pass
    passed = sorted(cases[test] for test in results.passed) if results.complete else []
    print(json.dumps({'passed': passed,
                      'cpu_seconds': time.process_time() - started}))


if __name__ == '__main__':
    main()
