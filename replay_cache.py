"""Compare executor plans and reference SQL for unchanged fixture datasets."""

import hashlib
import pickle
from concurrent.futures import Future


EXECUTOR_TESTS = {
    'tests/test_executor.py::TestExecutor::test_execute_tpch',
    'tests/test_executor.py::TestExecutor::test_execute_tpcds',
}


class InlineExecutor:
    def __init__(self, *, mp_context, initializer=None, initargs=()):
        if initializer is not None:
            initializer(*initargs)

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False

    def shutdown(self):
        pass

    def submit(self, task, *args, **kwargs):
        result = Future()
        try:
            task, args, kwargs = pickle.loads(pickle.dumps((task, args, kwargs)))
            result.set_result(pickle.loads(pickle.dumps(task(*args, **kwargs))))
        except Exception as error:
            result.set_exception(error)
        return result


def checkpoint_key(executor, plan):
    from sqlglot import exp
    from sqlglot.executor.table import Table
    from sqlglot.planner import Step

    steps = {}

    def freeze(value):
        if isinstance(value, exp.Expr):
            return ('expr', freeze(value.dump()))
        if isinstance(value, Step):
            identifier = id(value)
            if identifier not in steps:
                fields = {k: v for k, v in vars(value).items() if k != 'dependents'}
                signature = (type(value).__name__, freeze(fields))
                steps[identifier] = digest(signature)
            return ('step', steps[identifier])
        if isinstance(value, Table):
            return ('fixture-table', value.columns, repr(value.column_range))
        if isinstance(value, dict):
            return ('dict', tuple((freeze(k), freeze(v)) for k, v in value.items()))
        if isinstance(value, (tuple, list)):
            return (type(value).__name__, tuple(map(freeze, value)))
        if isinstance(value, (set, frozenset)):
            return (type(value).__name__, tuple(sorted(map(freeze, value), key=repr)))
        if isinstance(value, type) and value.__module__.startswith('sqlglot.'):
            return ('type', value.__module__, value.__qualname__)
        if type(value) in (bool, int, float, str, bytes, type(None)):
            return value
        raise ValueError('checkpoint input cannot be compared')

    return digest((freeze(plan.expression), freeze(plan.ctes), freeze(plan.root),
                   freeze(executor.tables.mapping)))


def digest(value):
    # Normalize memo references so equivalent values hash identically even if
    # their equal string and tuple objects are interned differently.
    return hashlib.sha256(repr(value).encode('utf-8')).hexdigest()
