#!/usr/bin/env node
/**
 * 只读解析 webui 的 .vue 单文件组件。
 *
 * 用 @vue/compiler-sfc 解析模板 AST，抽取按钮文案、tab / 表单项 / 表格列 / 弹窗标题，
 * 以及 script 里 ElMessage.* 的字符串字面量与 route.query 的参数名。
 * 绑定表达式（如 :label="expr"）一律输出 {"dynamic": "<源码片段>"}，不做任何推断。
 *
 * 用法：node vue.mjs <组件相对路径...>
 */

import fs from "node:fs";
import path from "node:path";
import { parse } from "@vue/compiler-sfc";

const ELEMENT = 1;
const TEXT = 2;
const INTERPOLATION = 5;
const BACKTICK = String.fromCharCode(96);
const QUOTES = ['"', "'", BACKTICK];
const QUOTE_CLASS = "[" + '"' + "'" + BACKTICK + "]";

function collapse(text) {
  return text.replace(/\s+/g, " ").trim();
}

function snippetOf(node, source) {
  return { dynamic: collapse(source.slice(node.loc.start.offset, node.loc.end.offset)) };
}

function assignedAttribute(element, name, source) {
  for (const prop of element.props) {
    if (prop.type === 6 && prop.name === name) {
      return prop.value ? prop.value.content : undefined;
    }
    if (prop.type === 7 && prop.name === "bind" && prop.arg && prop.arg.isStatic && prop.arg.content === name) {
      return prop.exp ? { dynamic: collapse(prop.exp.content) } : snippetOf(prop, source);
    }
  }
  return undefined;
}

function collectText(node, state) {
  for (const child of node.children || []) {
    if (child.type === TEXT) state.parts.push(child.content);
    else if (child.type === INTERPOLATION) state.dynamic = true;
    else if (child.type === ELEMENT) collectText(child, state);
  }
}

function textOf(element, source) {
  const state = { parts: [], dynamic: false };
  collectText(element, state);
  if (state.dynamic) return snippetOf(element, source);
  const text = collapse(state.parts.join(" "));
  return text ? text : undefined;
}

function walk(node, visit) {
  visit(node);
  for (const child of node.children || []) walk(child, visit);
}

function makeBuckets() {
  const buckets = { buttons: [], tabs: [], form_items: [], table_columns: [], dialogs: [] };
  const seen = new Map();
  return {
    add(name, value) {
      const serialized = JSON.stringify(value);
      if (!seen.has(name)) seen.set(name, new Set());
      if (seen.get(name).has(serialized)) return;
      seen.get(name).add(serialized);
      buckets[name].push(value);
    },
    result: buckets,
  };
}

const TAGS = [
  ["el-button", "buttons", "text", textOf],
  ["el-tab-pane", "tabs", "label", (element, source) => assignedAttribute(element, "label", source)],
  ["el-form-item", "form_items", "label", (element, source) => assignedAttribute(element, "label", source)],
  ["el-table-column", "table_columns", "label", (element, source) => assignedAttribute(element, "label", source)],
  ["el-dialog", "dialogs", "title", (element, source) => assignedAttribute(element, "title", source)],
];

function extractTemplate(descriptor, source) {
  const collector = makeBuckets();
  const ast = descriptor.template && descriptor.template.ast;
  if (!ast) return collector.result;
  walk(ast, (node) => {
    if (node.type !== ELEMENT) return;
    for (const [tag, bucket, field, read] of TAGS) {
      if (node.tag !== tag) continue;
      const value = read(node, source);
      if (value === undefined || value === null || value === "") continue;
      collector.add(bucket, { [field]: value });
    }
  });
  return collector.result;
}

function stripComments(text) {
  let out = "";
  let index = 0;
  while (index < text.length) {
    const char = text[index];
    if (QUOTES.includes(char)) {
      const start = index;
      index += 1;
      while (index < text.length && text[index] !== char) {
        if (text[index] === "\\") index += 1;
        index += 1;
      }
      index += 1;
      out += text.slice(start, index);
      continue;
    }
    if (char === "/" && text[index + 1] === "/") {
      while (index < text.length && text[index] !== "\n") index += 1;
      continue;
    }
    if (char === "/" && text[index + 1] === "*") {
      index += 2;
      while (index < text.length && !(text[index] === "*" && text[index + 1] === "/")) index += 1;
      index += 2;
      continue;
    }
    out += char;
    index += 1;
  }
  return out;
}

const ESCAPES = { n: "\n", t: "\t", r: "\r", "\\": "\\", '"': '"', "'": "'", [BACKTICK]: BACKTICK };

function readStringLiteral(text, start) {
  const quote = text[start];
  if (!QUOTES.includes(quote)) return null;
  let value = "";
  let index = start + 1;
  while (index < text.length) {
    const char = text[index];
    if (char === "\\") {
      const next = text[index + 1];
      value += Object.prototype.hasOwnProperty.call(ESCAPES, next) ? ESCAPES[next] : next;
      index += 2;
      continue;
    }
    if (char === quote) {
      if (quote === BACKTICK && value.includes("$" + "{")) return null;
      return { value };
    }
    value += char;
    index += 1;
  }
  return null;
}

function extractMessages(script) {
  const text = stripComments(script);
  const messages = [];
  const seen = new Set();
  const pattern = /ElMessage\s*(?:\.\s*([A-Za-z_$][\w$]*))?\s*\(\s*/g;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    const literal = readStringLiteral(text, match.index + match[0].length);
    if (!literal) continue;
    const value = collapse(literal.value);
    if (!value) continue;
    const type = match[1] === undefined ? "info" : match[1];
    const serialized = JSON.stringify({ type, text: value });
    if (seen.has(serialized)) continue;
    seen.add(serialized);
    messages.push({ type, text: value });
  }
  return messages;
}

function routeVariables(script) {
  const text = stripComments(script);
  const names = new Set();
  const direct = /(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*useRoute\(\s*\)/g;
  let match;
  while ((match = direct.exec(text)) !== null) names.add(match[1]);
  const destructured = /(?:const|let|var)\s*\{([^}]*)\}\s*=\s*useRoute\(\s*\)/g;
  while ((match = destructured.exec(text)) !== null) {
    for (const part of match[1].split(",")) {
      const pieces = part.split(":").map((piece) => piece.trim());
      if (pieces[0] === "query" && pieces[1]) names.add(pieces[1]);
      else if (pieces[0] === "query") names.add("query");
    }
  }
  return { text, names };
}

function blockEnd(text, openIndex) {
  let depth = 0;
  let index = openIndex;
  while (index < text.length) {
    const char = text[index];
    if (QUOTES.includes(char)) {
      index += 1;
      while (index < text.length && text[index] !== char) {
        if (text[index] === "\\") index += 1;
        index += 1;
      }
      index += 1;
      continue;
    }
    if (char === "{") depth += 1;
    else if (char === "}") {
      depth -= 1;
      if (depth === 0) return index;
    }
    index += 1;
  }
  return text.length;
}

/**
 * 页面常把读参数包成本地小函数：const getQuery = (key) => route.query[key]，
 * 再 getQuery("graph_definition_id")。只认「参数就是 route.query 的下标」且调用处是字符串字面量的情形，
 * 其余一律不推断。
 */
function queryHelpers(text, names) {
  const helpers = new Map();
  const declarations = [
    /(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\(([^)]*)\)|([A-Za-z_$][\w$]*))\s*=>/g,
    /function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{/g,
  ];
  for (const pattern of declarations) {
    let match;
    while ((match = pattern.exec(text)) !== null) {
      const paramsText = pattern.source.startsWith("function") ? match[2] : match[2] || match[3];
      const param = String(paramsText || "").split(",")[0].split(":")[0].trim();
      if (!/^[A-Za-z_$][\w$]*$/.test(param)) continue;
      let bodyStart = match.index + match[0].length;
      if (text[bodyStart - 1] === "{") bodyStart -= 1;
      while (bodyStart < text.length && /\s/.test(text[bodyStart])) bodyStart += 1;
      const semiIndex = text.indexOf(";", bodyStart);
      const body =
        text[bodyStart] === "{"
          ? text.slice(bodyStart, blockEnd(text, bodyStart) + 1)
          : text.slice(bodyStart, semiIndex === -1 ? text.length : semiIndex);
      for (const name of names) {
        const indexed = new RegExp("\\b" + name + "\\.query\\[\\s*" + param + "\\s*\\]");
        if (indexed.test(body)) helpers.set(match[1], param);
      }
    }
  }
  return helpers;
}

function helperQueryKeys(text, helpers) {
  const keys = new Set();
  for (const name of helpers.keys()) {
    const calls = new RegExp("\\b" + name + "\\s*\\(\\s*" + QUOTE_CLASS, "g");
    let match;
    while ((match = calls.exec(text)) !== null) {
      const literal = readStringLiteral(text, match.index + match[0].length - 1);
      if (literal && /^[A-Za-z_$][\w$]*$/.test(literal.value)) keys.add(literal.value);
    }
  }
  return keys;
}

function extractQueryKeys(script) {
  const { text, names } = routeVariables(script);
  const keys = new Set();
  let match;
  for (const name of names) {
    const dotted = new RegExp("\\b" + name + "\\.query\\.([A-Za-z_$][\\w$]*)", "g");
    while ((match = dotted.exec(text)) !== null) keys.add(match[1]);
    const indexed = new RegExp("\\b" + name + "\\.query\\[\\s*" + QUOTE_CLASS + "([A-Za-z_$][\\w$]*)" + QUOTE_CLASS + "\\s*\\]", "g");
    while ((match = indexed.exec(text)) !== null) keys.add(match[1]);
  }
  for (const key of helperQueryKeys(text, queryHelpers(text, names))) keys.add(key);
  const vueRoute = /\bthis\.\$route\.query\.([A-Za-z_$][\w$]*)/g;
  while ((match = vueRoute.exec(text)) !== null) keys.add(match[1]);
  return [...keys].sort();
}

function extractFile(file) {
  const source = fs.readFileSync(file, "utf8");
  const { descriptor, errors } = parse(source, { filename: file });
  if (errors.length) throw new Error(file + " 解析失败：" + errors[0].message);
  const template = extractTemplate(descriptor, source);
  const script = [descriptor.script && descriptor.script.content, descriptor.scriptSetup && descriptor.scriptSetup.content]
    .filter(Boolean)
    .join("\n");
  return {
    file: file.split(path.sep).join("/"),
    buttons: template.buttons,
    tabs: template.tabs,
    form_items: template.form_items,
    table_columns: template.table_columns,
    dialogs: template.dialogs,
    messages: extractMessages(script),
    query_keys: extractQueryKeys(script),
  };
}

function main(argv) {
  const files = [...new Set(argv)];
  if (!files.length) {
    process.stderr.write("用法：node vue.mjs <组件相对路径...>\n");
    return 2;
  }
  const result = { files: files.map((file) => extractFile(file)) };
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  return 0;
}

try {
  process.exitCode = main(process.argv.slice(2));
} catch (error) {
  process.stderr.write(String(error && error.message ? error.message : error) + "\n");
  process.exitCode = 1;
}
