"""Import-bound patch classification shared by the extraction guards."""

import ast


def _scope_imports(statements):
    bindings = {}

    class Imports(ast.NodeVisitor):
        def bind(self, name, module):
            if name in bindings and bindings[name] != module:
                bindings[name] = None
            else:
                bindings[name] = module

        def visit_Import(self, node):
            for alias in node.names:
                self.bind(alias.asname or alias.name.split(".")[0],
                          alias.name if alias.asname else alias.name.split(".")[0])

        def visit_ImportFrom(self, node):
            for alias in node.names:
                if alias.name != "*":
                    self.bind(alias.asname or alias.name,
                              node.module + "." + alias.name
                              if node.module and not node.level else None)

        def visit_FunctionDef(self, node):
            pass

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

    visitor = Imports()
    for statement in statements:
        visitor.visit(statement)
    return bindings


def patch_targets(tree, names):
    """Yield (line, target, member, module) for the extraction guards.

    Name targets resolve only through module-level and function-level imports:
    ``import M``, ``import M as A``, ``from P import M``, and
    ``from P import M as A``. From-imports retain the full ``P.M`` identity.
    Positional string-literal patch.object attributes use that import binding.
    Keyword or starred target/attribute arguments yield module None. A positional
    non-literal attribute yields module None only for a target imported from
    ``swarm`` or ``swarm_*``; other such calls are not reported.
    Function imports shadow outer imports; conflicting imports in one scope
    are unresolved. No assignment aliases, attribute traversal, dynamic loads,
    relative/star imports, class imports, or control flow are interpreted.
    For a literal member in names, a target without an import binding yields
    module None and must fail the consuming guard as unresolved. Only import
    bindings are interpreted: a local variable holding the facade module is
    out of contract.

    Recognized mutations are literal-member patch.object, builtin setattr,
    attribute assignment, and literal-string patch calls. A patch string is
    an explicit module path, not a local variable name, so it retains that
    module identity even if a local import uses a conflicting alias.
    """
    def visit(node, bindings):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local = dict(bindings)
            local.update(_scope_imports(node.body))
            for field, value in ast.iter_fields(node):
                children = value if isinstance(value, list) else [value]
                for child in children:
                    if isinstance(child, ast.AST):
                        yield from visit(child, local if field == "body" else bindings)
            return

        target, member, literal = None, None, False
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "object"):
            if (len(node.args) < 2
                    or any(isinstance(arg, ast.Starred) for arg in node.args[:2])):
                yield node.lineno, ast.unparse(node), None, None
                return
            if (not isinstance(node.args[1], ast.Constant)
                    or not isinstance(node.args[1].value, str)):
                module = (bindings.get(node.args[0].id)
                          if isinstance(node.args[0], ast.Name) else None)
                if module and (module == "swarm" or module.startswith("swarm_")):
                    yield node.lineno, ast.unparse(node), None, None
                    return
        if (isinstance(node, ast.Call) and len(node.args) >= 2
                and ((isinstance(node.func, ast.Attribute)
                      and node.func.attr == "object")
                     or (isinstance(node.func, ast.Name)
                         and node.func.id == "setattr"))
                and isinstance(node.args[1], ast.Constant)):
            target, member = node.args[0], node.args[1].value
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            target, member = node.value, node.attr
        elif (isinstance(node, ast.Call) and node.args
              and ((isinstance(node.func, ast.Attribute) and node.func.attr == "patch")
                   or (isinstance(node.func, ast.Name) and node.func.id == "patch"))
              and isinstance(node.args[0], ast.Constant)
              and isinstance(node.args[0].value, str)):
            target, _, member = node.args[0].value.rpartition(".")
            literal = True
        if member in names:
            if literal:
                module, spelling = target or None, target
            else:
                module = bindings.get(target.id) if isinstance(target, ast.Name) else None
                spelling = ast.unparse(target)
            yield node.lineno, spelling, member, module
        for child in ast.iter_child_nodes(node):
            yield from visit(child, bindings)

    yield from visit(tree, _scope_imports(tree.body))
