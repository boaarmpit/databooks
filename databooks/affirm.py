"""Functions to safely evaluate strings and inspect notebook."""

import ast
from copy import deepcopy
from functools import reduce
from itertools import compress
from pathlib import Path
from typing import Any, Dict, Iterable, List, Union

from databooks import JupyterNotebook
from databooks.common import get_keys
from databooks.data_models.base import DatabooksBase
from databooks.logging import get_logger, set_verbose
from databooks.recipes import Recipe

logger = get_logger(__file__)

_ALLOWED_BUILTINS = (all, any, enumerate, filter, hasattr, len, list, range, sorted)
_ALLOWED_NODES = (
    ast.Add,
    ast.And,
    ast.BinOp,
    ast.BitAnd,
    ast.BitOr,
    ast.BitXor,
    ast.BoolOp,
    ast.boolop,
    ast.cmpop,
    ast.Compare,
    ast.comprehension,
    ast.Constant,
    ast.Dict,
    ast.Eq,
    ast.Expr,
    ast.expr,
    ast.expr_context,
    ast.Expression,
    ast.For,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.Is,
    ast.IsNot,
    ast.List,
    ast.ListComp,
    ast.Load,
    ast.LShift,
    ast.Lt,
    ast.LtE,
    ast.Mod,
    ast.Name,
    ast.Not,
    ast.NotEq,
    ast.NotIn,
    ast.Num,
    ast.operator,
    ast.Or,
    ast.RShift,
    ast.Set,
    ast.Slice,
    ast.slice,
    ast.Str,
    ast.Sub,
    ast.Tuple,
    ast.UAdd,
    ast.UnaryOp,
    ast.unaryop,
    ast.USub,
)


class DatabooksParser(ast.NodeVisitor):
    """AST parser that disallows unsafe nodes/values."""

    def __init__(self, **variables: Any) -> None:
        """Instantiate with variables and callables (built-ins) scope."""
        # https://github.com/python/mypy/issues/3728
        self.builtins = {b.__name__: b for b in _ALLOWED_BUILTINS}  # type: ignore
        self.names = deepcopy(variables) or {}
        self.scope = {
            **self.names,
            "__builtins__": self.builtins,
        }

    def _get_iter(self, node: ast.AST) -> Iterable:
        """Use `DatabooksParser.safe_eval_ast` to get the iterable object."""
        tree = ast.Expression(body=node)
        return iter(self.safe_eval_ast(tree))

    def generic_visit(self, node: ast.AST) -> None:
        """
        Prioritize `ast.comprehension` nodes when expanding tree.

        Similar to `NodeVisitor.generic_visit`, but favor comprehensions when multiple
         nodes on the same level. In comprehensions, we have a generator argument that
         includes names that are stored. By visiting them first we avoid 'running into'
         unknown names.
        """
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"Invalid node `{node}`.")

        for field, value in sorted(
            ast.iter_fields(node), key=lambda f: f[0] != "generators"
        ):
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        self.visit(item)
            elif isinstance(value, ast.AST):
                self.visit(value)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        """Add variable from a comprehension to list of allowed names."""
        if not isinstance(node.target, ast.Name):
            raise RuntimeError(
                "Expected `ast.comprehension`'s target to be `ast.Name`, got"
                f" `ast.{type(node.target).__name__}`."
            )
        # If any elements in the comprehension are a `DatabooksBase` instance, then
        #  pass down the attributes as valid
        iterable = self._get_iter(node.iter)
        databooks_el = [el for el in iterable if isinstance(el, DatabooksBase)]
        if databooks_el:
            d_attrs = reduce(lambda a, b: {**a, **b}, [dict(el) for el in databooks_el])
        self.names[node.target.id] = DatabooksBase(**d_attrs) if databooks_el else ...
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Allow attributes for Pydantic fields only."""
        if not isinstance(node.value, (ast.Attribute, ast.Name)):
            raise ValueError(
                "Expected attribute to be one of `ast.Name` or `ast.Attribute`, got"
                f" `ast.{type(node.value).__name__}`"
            )
        if not isinstance(node.value, ast.Attribute):
            obj = self.names[node.value.id]
            allowed_attrs = (
                get_keys(obj.dict()) if isinstance(obj, DatabooksBase) else ()
            )
            if node.attr not in allowed_attrs:
                raise ValueError(
                    "Expected attribute to be one of"
                    f" `{allowed_attrs}`, got `{node.attr}`"
                )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """Only allow names from scope or comprehension variables."""
        valid_names = {**self.names, **self.builtins}
        if node.id not in valid_names:
            raise ValueError(
                f"Expected `name` to be one of `{valid_names.keys()}`, got `{node.id}`."
            )
        self.generic_visit(node)

    def safe_eval_ast(self, ast_tree: ast.AST) -> Any:
        """Evaluate safe AST trees only (raise errors otherwise)."""
        self.visit(ast_tree)
        exe = compile(ast_tree, filename="", mode="eval")
        return eval(exe, self.scope)

    def safe_eval(self, src: str) -> Any:
        """
        Evaluate strings that are safe only (raise errors otherwise).

        A "safe" string or node provided may only consist of nodes in
         `databooks.affirm._ALLOWED_NODES` and built-ins from
         `databooks.affirm._ALLOWED_BUILTINS`.
        """
        ast_tree = ast.parse(src, mode="eval")
        return self.safe_eval_ast(ast_tree)


def affirm(
    nb_path: Path, exprs: List[Union[str, Recipe]], verbose: bool = False
) -> bool:
    """
    Return whether notebook passed all checks (expressions).

    :param nb_path: Path of notebook file
    :param exprs: Expression with check to be evaluated on notebook
    :param verbose: Log failed tests for notebook
    :return: Evaluated expression casted as a `bool`
    """
    if verbose:
        set_verbose(logger)

    nb = JupyterNotebook.parse_file(nb_path)
    variables: Dict[str, Any] = {
        "nb": nb,
        "raw_cells": [c for c in nb.cells if c.cell_type == "raw"],
        "markdown_cells": [c for c in nb.cells if c.cell_type == "markdown"],
        "code_cells": [c for c in nb.cells if c.cell_type == "code"],
        "exec_cells": [
            c
            for c in nb.cells
            if c.cell_type == "code" and c.execution_count is not None
        ],
    }
    databooks_parser = DatabooksParser(**variables)
    is_ok = [
        bool(
            databooks_parser.safe_eval(
                expr if isinstance(expr, str) else expr.src,
            )
        )
        for expr in exprs
    ]
    n_fail = sum([not ok for ok in is_ok])
    logger.info(f"{nb_path} failed {n_fail} of {len(is_ok)} checks.")
    logger.debug(
        str(nb_path)
        + (
            f" failed {list(compress(exprs, (not ok for ok in is_ok)))}."
            if n_fail > 0
            else " succeeded all checks."
        )
    )
    return all(is_ok)
