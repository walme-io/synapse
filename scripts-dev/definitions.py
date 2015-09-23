#! /usr/bin/python

import ast
import yaml

class DefinitionVisitor(ast.NodeVisitor):
    def __init__(self):
        super(DefinitionVisitor, self).__init__()
        self.functions = {}
        self.classes = {}
        self.names = {}
        self.attrs = set()
        self.definitions = {
            'def': self.functions,
            'class': self.classes,
            'names': self.names,
            'attrs': self.attrs,
        }

    def visit_Name(self, node):
        self.names.setdefault(type(node.ctx).__name__, set()).add(node.id)

    def visit_Attribute(self, node):
        self.attrs.add(node.attr)
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def visit_ClassDef(self, node):
        visitor = DefinitionVisitor()
        self.classes[node.name] = visitor.definitions
        for child in ast.iter_child_nodes(node):
            visitor.visit(child)

    def visit_FunctionDef(self, node):
        visitor = DefinitionVisitor()
        self.functions[node.name] = visitor.definitions
        for child in ast.iter_child_nodes(node):
            visitor.visit(child)


def non_empty(defs):
    functions = {name: non_empty(f) for name, f in defs['def'].items()}
    classes = {name: non_empty(f) for name, f in defs['class'].items()}
    result = {}
    if functions: result['def'] = functions
    if classes: result['class'] = classes
    names = defs['names']
    uses = []
    for name in names.get('Load', ()):
        if name not in names.get('Param', ()) and name not in names.get('Store', ()):
            uses.append(name)
    uses.extend(defs['attrs'])
    if uses: result['uses'] = uses
    result['names'] = names
    result['attrs'] = defs['attrs']
    return result


def definitions_in_code(input_code):
    input_ast = ast.parse(input_code)
    visitor = DefinitionVisitor()
    visitor.visit(input_ast)
    definitions = non_empty(visitor.definitions)
    return definitions


def definitions_in_file(filepath):
    with open(filepath) as f:
        return definitions_in_code(f.read())


def defined_names(prefix, defs, names):
    for name, funcs in defs.get('def', {}).items():
        names.setdefault(name, {'defined': []})['defined'].append(prefix + name)
        defined_names(prefix + name + ".", funcs, names)

    for name, funcs in defs.get('class', {}).items():
        names.setdefault(name, {'defined': []})['defined'].append(prefix + name)
        defined_names(prefix + name + ".", funcs, names)


def used_names(prefix, defs, names):
    for name, funcs in defs.get('def', {}).items():
        used_names(prefix + name + ".", funcs, names)

    for name, funcs in defs.get('class', {}).items():
        used_names(prefix + name + ".", funcs, names)

    for used in defs.get('uses', ()):
        if used in names:
            names[used].setdefault('used', []).append(prefix.rstrip('.'))


if __name__ == '__main__':
    import sys, os, argparse, re

    parser = argparse.ArgumentParser(description='Find definitions.')
    parser.add_argument(
        "--unused", action="store_true", help="Only list unused definitions"
    )
    parser.add_argument(
        "--ignore", action="append", metavar="REGEXP", help="Ignore a pattern"
    )
    parser.add_argument(
        "--pattern", nargs='+', action="append", metavar="REGEXP",
        help="Search for a pattern"
    )
    parser.add_argument(
        "directories", nargs='+', metavar="DIR",
        help="Directories to search for definitions"
    )
    args = parser.parse_args()

    definitions = {}
    for directory in args.directories:
        for root, dirs, files in os.walk(directory):
            for filename in files:
                if filename.endswith(".py"):
                    filepath = os.path.join(root, filename)
                    definitions[filepath] = definitions_in_file(filepath)

    names = {}
    for filepath, defs in definitions.items():
        defined_names(filepath + ":", defs, names)

    for filepath, defs in definitions.items():
        used_names(filepath + ":", defs, names)

    patterns = [re.compile(pattern) for pattern in args.pattern or ()]
    ignore = [re.compile(pattern) for pattern in args.ignore or ()]

    result = {}
    for name, definition in names.items():
        if patterns and not any(pattern.match(name) for pattern in patterns):
            continue
        if ignore and any(pattern.match(name) for pattern in ignore):
            continue
        if args.unused and definition.get('used'):
            continue
        result[name] = definition

    yaml.dump(result, sys.stdout, default_flow_style=False)
