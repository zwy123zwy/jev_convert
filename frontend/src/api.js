// 后端 API 访问封装：统一 JSON 解析与错误形状。
export async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  return { ok: response.ok, data };
}

export function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
