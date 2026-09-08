"""The registry of write-path producers every comparability test must cover.

It exists because the previous lists were denylists by omission.
`test_both_loaders_dispatch_through_the_shared_driver` and
`test_neither_loader_builds_its_own_header` were parametrized over
`[scylla_load, opensearch_load]`, and `churn_load` violated both — it owned a
`ThreadPoolExecutor` and built its own `runmeta.header` — while simply not
being in the list. Nothing failed, so a hardcoded `IN_FLIGHT = 4` shipped a
published engine claim.

`test_the_registry_covers_every_write_path_module` closes it: the names are
checked against what is actually on disk, so a new producer is covered the day
it lands rather than the day someone remembers to add it.
"""
import ast
import importlib
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = BENCH_DIR / "ftsbench"

WRITE_PATH_PRODUCERS = ("opensearch_load", "scylla_load", "churn_load",
                        "mp_load")


def producer_modules() -> list:
    return [importlib.import_module(f"ftsbench.{name}")
            for name in WRITE_PATH_PRODUCERS]


def producer_source(name: str) -> str:
    return (PACKAGE_DIR / f"{name}.py").read_text(encoding="utf-8")


def modules_on_disk() -> set[str]:
    return {path.stem for path in PACKAGE_DIR.glob("*_load.py")}


def referenced_names(name: str) -> set[str]:
    """Every identifier the module's CODE mentions, ignoring prose.

    A substring search over the file cannot be used: these modules carry
    docstrings that name the very machinery they must not use, because
    recording why a defect was removed is how it stays removed. Parsing means
    the prose can say `ThreadPoolExecutor` while the assertion still bites on a
    real one.
    """
    tree = ast.parse(producer_source(name))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {alias.name.split(".")[0] for alias in node.names}
    return names


def calls_qualified(name: str, dotted: str) -> bool:
    """Whether the module's code calls `dotted`, e.g. `load_driver.run_timed`."""
    module_name, _, attribute = dotted.rpartition(".")
    for node in ast.walk(ast.parse(producer_source(name))):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (isinstance(function, ast.Attribute) and function.attr == attribute
                and isinstance(function.value, ast.Name)
                and function.value.id == module_name):
            return True
    return False
