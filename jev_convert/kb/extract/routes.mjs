#!/usr/bin/env node
/**
 * 只读解析 webui 的路由表与 V2 菜单编排。
 *
 * 用 TypeScript 编译器 API 读语法树，绝不执行被测代码；遇到不是字面量的值一律输出
 * {"dynamic": "<源码片段>"}，不做任何推断。
 *
 * 用法：
 *   node routes.mjs <webui 目录> [--modules src/config/v2Modules.ts]
 *                                 [--export V2_MODULES]
 *                                 [--out 输出文件]
 */

import fs from "node:fs";
import path from "node:path";
import ts from "typescript";

const DYNAMIC = "dynamic";
const ROUTES_FILE = "src/router/routes.ts";
const ROUTES_EXPORT = "routes";

function dynamicOf(node) {
  const text = node.getText().replace(/\s+/g, " ").trim();
  return { [DYNAMIC]: text };
}

function propertyKey(name) {
  if (!name) return null;
  if (ts.isIdentifier(name) || ts.isStringLiteral(name) || ts.isNumericLiteral(name)) return name.text;
  return null;
}

function propsOf(node) {
  const props = new Map();
  for (const prop of node.properties) {
    if (ts.isPropertyAssignment(prop)) {
      const key = propertyKey(prop.name);
      if (key) props.set(key, prop.initializer);
    } else if (ts.isShorthandPropertyAssignment(prop)) {
      props.set(prop.name.text, prop.name);
    }
  }
  return props;
}

function literalValue(node) {
  if (!node) return null;
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  if (ts.isNumericLiteral(node)) return Number(node.text);
  if (node.kind === ts.SyntaxKind.TrueKeyword) return true;
  if (node.kind === ts.SyntaxKind.FalseKeyword) return false;
  if (node.kind === ts.SyntaxKind.NullKeyword) return null;
  if (ts.isPrefixUnaryExpression(node) && node.operator === ts.SyntaxKind.MinusToken && ts.isNumericLiteral(node.operand)) {
    return -Number(node.operand.text);
  }
  if (ts.isArrayLiteralExpression(node)) return node.elements.map((element) => literalValue(element));
  if (ts.isObjectLiteralExpression(node)) return objectValue(node);
  if (ts.isParenthesizedExpression(node)) return literalValue(node.expression);
  return dynamicOf(node);
}

function objectValue(node) {
  const out = {};
  for (const [key, value] of propsOf(node)) out[key] = literalValue(value);
  return out;
}

function posixRelative(root, absolute) {
  return path.relative(root, absolute).split(path.sep).join("/");
}

function collectImports(sourceFile) {
  const imports = new Map();
  for (const statement of sourceFile.statements) {
    if (!ts.isImportDeclaration(statement) || !ts.isStringLiteral(statement.moduleSpecifier)) continue;
    const specifier = statement.moduleSpecifier.text;
    const clause = statement.importClause;
    if (!clause) continue;
    if (clause.name) imports.set(clause.name.text, specifier);
    if (clause.namedBindings && ts.isNamedImports(clause.namedBindings)) {
      for (const element of clause.namedBindings.elements) imports.set(element.name.text, specifier);
    } else if (clause.namedBindings && ts.isNamespaceImport(clause.namedBindings)) {
      imports.set(clause.namedBindings.name.text, specifier);
    }
  }
  return imports;
}

function findExportedValue(sourceFile, name) {
  for (const statement of sourceFile.statements) {
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (ts.isIdentifier(declaration.name) && declaration.name.text === name) return declaration.initializer ?? null;
    }
  }
  return null;
}

function makeContext(root, sourceFile) {
  return {
    root,
    dir: path.dirname(sourceFile.fileName),
    imports: collectImports(sourceFile),
  };
}

function resolveSpecifier(specifier, context) {
  if (typeof specifier !== "string") return specifier;
  if (specifier.startsWith("@/")) return posixRelative(context.root, path.join(context.root, "src", specifier.slice(2)));
  if (specifier.startsWith(".")) return posixRelative(context.root, path.resolve(context.dir, specifier));
  return specifier;
}

function fromExpression(node, context) {
  if (!node) return null;
  if (ts.isParenthesizedExpression(node)) return fromExpression(node.expression, context);
  if ((ts.isArrowFunction(node) || ts.isFunctionExpression(node)) && !ts.isBlock(node.body)) {
    return fromExpression(node.body, context);
  }
  if (ts.isCallExpression(node)) {
    if (node.expression.kind === ts.SyntaxKind.ImportKeyword) {
      const argument = node.arguments[0];
      return argument && ts.isStringLiteral(argument) ? resolveSpecifier(argument.text, context) : dynamicOf(argument ?? node);
    }
    if (node.arguments.length === 1) {
      const inner = fromExpression(node.arguments[0], context);
      if (typeof inner === "string" || (inner && inner[DYNAMIC] !== undefined)) return inner;
    }
    return dynamicOf(node);
  }
  if (ts.isIdentifier(node)) {
    const specifier = context.imports.get(node.text);
    return specifier ? resolveSpecifier(specifier, context) : dynamicOf(node);
  }
  if (ts.isStringLiteral(node)) return resolveSpecifier(node.text, context);
  return dynamicOf(node);
}

function joinPath(parent, child) {
  if (typeof child !== "string") return child;
  if (child.startsWith("/")) return child.length > 1 ? child.replace(/\/+$/, "") : child;
  const base = typeof parent === "string" && parent !== "/" ? parent.replace(/\/+$/, "") : "";
  return base + "/" + child;
}

function routeOf(node, parentPath, context, depth) {
  if (!ts.isObjectLiteralExpression(node)) return null;
  const props = propsOf(node);
  const pathNode = props.get("path");
  const routePath = pathNode ? literalValue(pathNode) : null;
  const fullPath = pathNode ? joinPath(parentPath, routePath) : parentPath;
  const record = {
    path: routePath,
    full_path: fullPath,
    name: props.has("name") ? literalValue(props.get("name")) : null,
    meta: props.has("meta") ? literalValue(props.get("meta")) : {},
    redirect: props.has("redirect") ? literalValue(props.get("redirect")) : null,
    component: fromExpression(props.get("component"), context),
    props: props.has("props") ? literalValue(props.get("props")) : null,
    depth,
    children: [],
  };
  const children = props.get("children");
  if (children && ts.isArrayLiteralExpression(children)) {
    for (const child of children.elements) {
      const sub = routeOf(child, fullPath, context, depth + 1);
      if (sub) record.children.push(sub);
    }
  }
  return record;
}

function leafOf(node) {
  const props = propsOf(node);
  return {
    backend_key: props.has("backendKey") ? literalValue(props.get("backendKey")) : null,
    title: props.has("title") ? literalValue(props.get("title")) : null,
  };
}

function entryOf(node) {
  if (!ts.isObjectLiteralExpression(node)) return { kind: "unknown", dynamic: dynamicOf(node) };
  const props = propsOf(node);
  if (props.has("leaves")) {
    const leaves = props.get("leaves");
    return {
      kind: "container",
      key: props.has("key") ? literalValue(props.get("key")) : null,
      title: props.has("title") ? literalValue(props.get("title")) : null,
      name: props.has("name") ? literalValue(props.get("name")) : null,
      from_backend_key: props.has("fromBackendKey") ? literalValue(props.get("fromBackendKey")) : null,
      icon: props.has("icon") ? literalValue(props.get("icon")) : null,
      leaves:
        leaves && ts.isArrayLiteralExpression(leaves)
          ? leaves.elements.map((leaf) => leafOf(leaf))
          : leaves
            ? [leafOf(leaves)]
            : [],
    };
  }
  return { kind: "leaf", ...leafOf(node) };
}

function moduleOf(node) {
  const props = propsOf(node);
  const entries = props.get("entries");
  return {
    key: props.has("key") ? literalValue(props.get("key")) : null,
    name: props.has("name") ? literalValue(props.get("name")) : null,
    icon: props.has("icon") ? literalValue(props.get("icon")) : null,
    home_backend_key: props.has("homeBackendKey") ? literalValue(props.get("homeBackendKey")) : null,
    entries: entries && ts.isArrayLiteralExpression(entries) ? entries.elements.map((entry) => entryOf(entry)) : [],
  };
}

function load(file) {
  const text = fs.readFileSync(file, "utf8");
  return ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
}

function parseArgs(argv) {
  const options = { root: null, modules: "src/config/v2Modules.ts", exportName: "V2_MODULES", out: null };
  const rest = [];
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--modules" || arg === "--export" || arg === "--out") {
      const value = argv[index + 1];
      index += 1;
      if (arg === "--modules") options.modules = value;
      else if (arg === "--export") options.exportName = value;
      else options.out = value;
    } else if (arg.startsWith("--modules=")) options.modules = arg.slice("--modules=".length);
    else if (arg.startsWith("--export=")) options.exportName = arg.slice("--export=".length);
    else if (arg.startsWith("--out=")) options.out = arg.slice("--out=".length);
    else rest.push(arg);
  }
  options.root = rest[0] ?? null;
  return options;
}

function main(argv) {
  const options = parseArgs(argv);
  if (!options.root) {
    process.stderr.write("用法：node routes.mjs <webui 目录> [--modules <相对路径>] [--export <导出名>] [--out <文件>]\n");
    return 2;
  }
  const root = path.resolve(options.root);
  const routesFile = path.join(root, ROUTES_FILE);
  if (!fs.existsSync(routesFile)) {
    process.stderr.write("找不到路由文件：" + ROUTES_FILE + "\n");
    return 1;
  }

  const routesSource = load(routesFile);
  const routesContext = makeContext(root, routesSource);
  const routesArray = findExportedValue(routesSource, ROUTES_EXPORT);
  const routes = [];
  if (routesArray && ts.isArrayLiteralExpression(routesArray)) {
    for (const element of routesArray.elements) {
      const route = routeOf(element, "", routesContext, 0);
      if (route) routes.push(route);
    }
  }

  const modules = [];
  const modulesFile = path.join(root, options.modules);
  if (fs.existsSync(modulesFile)) {
    const modulesSource = load(modulesFile);
    const modulesArray = findExportedValue(modulesSource, options.exportName);
    if (modulesArray && ts.isArrayLiteralExpression(modulesArray)) {
      for (const element of modulesArray.elements) modules.push(moduleOf(element));
    }
  }

  const document = {
    routes_file: ROUTES_FILE,
    modules_file: options.modules,
    modules_export: options.exportName,
    routes,
    modules,
  };
  const text = JSON.stringify(document, null, 2) + "\n";
  if (options.out) fs.writeFileSync(options.out, text);
  else process.stdout.write(text);
  return 0;
}

process.exitCode = main(process.argv.slice(2));
