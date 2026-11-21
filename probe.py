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
    def __init__(self):
        super().__init__()
        self.passed = set()
        self.complete = True

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


def main():
    started = time.process_time()
    root = Path(sys.argv[1])
    request = json.load(sys.stdin)
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.meta_path.insert(0, Overlay(root, request['files']))
    os.environ['SKIP_INTEGRATION'] = '0'

    results = Results()
    with open(os.devnull, 'w', encoding='utf-8') as sink, contextlib.redirect_stdout(sink):
        cases = unit_tests(request['tests'])
        unittest.TestSuite(cases).run(results)
    passed = sorted(cases[test] for test in results.passed) if results.complete else []
    print(json.dumps({'passed': passed,
                      'cpu_seconds': time.process_time() - started}))


if __name__ == '__main__':
    main()
