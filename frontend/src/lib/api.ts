// Thin JSON client for the Python API (same origin; Vite proxies /api in dev).

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  let data: any = text;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    /* plain text */
  }
  if (!r.ok) throw new ApiError(r.status, (data && data.detail) || r.statusText);
  return data as T;
}

export const api = {
  get: <T = any>(url: string) => req<T>("GET", url),
  post: <T = any>(url: string, body: unknown = {}) => req<T>("POST", url, body),
  put: <T = any>(url: string, body: unknown) => req<T>("PUT", url, body),
  patch: <T = any>(url: string, body: unknown) => req<T>("PATCH", url, body),
  del: <T = any>(url: string) => req<T>("DELETE", url),
};
