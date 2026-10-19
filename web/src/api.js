import axios from "axios";

const TOKEN_KEY = "tdp_token";
const ROLE_KEY = "tdp_role";
const NAME_KEY = "tdp_name";

export const session = {
  token: () => localStorage.getItem(TOKEN_KEY) || "",
  role: () => localStorage.getItem(ROLE_KEY) || "",
  name: () => localStorage.getItem(NAME_KEY) || "",
  save(data) {
    localStorage.setItem(TOKEN_KEY, data.access_token);
    localStorage.setItem(ROLE_KEY, data.role || "");
    localStorage.setItem(NAME_KEY, data.username || "");
  },
  clear() {
    [TOKEN_KEY, ROLE_KEY, NAME_KEY].forEach((k) => localStorage.removeItem(k));
  },
};

const http = axios.create({ timeout: 15000 });

http.interceptors.request.use((config) => {
  const token = session.token();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// 把后端统一的 {error, detail, trace_id} 抹平成一句人话，
// 这样每个页面不用各自解析错误结构（也顺便把 trace_id 留给排查用）。
http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    const resp = error.response;
    if (!resp) {
      error.friendly = "网络异常，检查后端是否已启动";
      return Promise.reject(error);
    }
    const body = resp.data || {};
    const detail = body.detail;
    let text = body.error || `HTTP ${resp.status}`;
    if (typeof detail === "string" && detail) text = detail;
    else if (detail && detail.reason) text = detail.reason;
    else if (Array.isArray(detail)) text = detail.map((d) => d.msg || d).join("；");
    if (resp.status === 401) {
      session.clear();
      text = "登录已过期，请重新登录";
    }
    error.friendly = text;
    error.traceId = body.trace_id;
    return Promise.reject(error);
  }
);

const idem = () =>
  (crypto.randomUUID && crypto.randomUUID()) ||
  `${Date.now()}-${Math.random().toString(16).slice(2)}`;

export const urlParam = (key) => new URLSearchParams(location.search).get(key);

export const api = {
  login: (username, password) =>
    http.post("/v1/auth/token", { username, password }).then((r) => r.data),
  options: () => http.get("/v1/meta/options").then((r) => r.data),
  slots: (vin, date) =>
    http.get(`/v1/vehicles/${encodeURIComponent(vin)}/slots`, { params: { date } }).then((r) => r.data),
  book: (payload) =>
    http.post("/v1/bookings", payload, { headers: { "Idempotency-Key": idem() } }).then((r) => r.data),
  queue: (params) => http.get("/v1/queue", { params }).then((r) => r.data),
  claim: (payload) =>
    http.post("/v1/queue/claim", payload, { headers: { "Idempotency-Key": idem() } }).then((r) => r.data),
  daily: (bizDate, projectId) =>
    http
      .get("/v1/reports/daily", { params: { biz_date: bizDate, project_id: projectId || undefined } })
      .then((r) => r.data),
  defects: (params) => http.get("/v1/defects", { params }).then((r) => r.data),
  health: () => http.get("/readyz").then((r) => r.data),
  me: () => http.get("/v1/auth/me").then((r) => r.data),
};
