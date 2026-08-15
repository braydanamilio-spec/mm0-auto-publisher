/**
 * MM0 Connect Worker — Cloudflare Worker (FREE).
 *
 * Nhiệm vụ: cung cấp nút "Kết nối kênh" trên dashboard.
 *   /auth/start?channel=BROKE&kind=youtube  -> chuyển tới màn Cho phép của Google
 *   /auth/callback                          -> đổi code lấy REFRESH TOKEN, lưu Firestore
 *
 * Token lưu ở Firestore collection "connections/{channel}_{kind}" (rules chặn client đọc).
 * Pipeline (main.py/stats.py) đọc token từ đây bằng service account -> đăng bình thường.
 *
 * Secrets cần đặt (wrangler secret put):
 *   YT_CLIENT_ID, YT_CLIENT_SECRET   (OAuth client loại "Web application")
 *   SA_CLIENT_EMAIL, SA_PRIVATE_KEY  (service account để ghi Firestore)
 *   FIREBASE_PROJECT_ID
 *   ALLOW_EMAIL (tuỳ chọn) — chỉ email này mới được kết nối (bảo vệ)
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (url.pathname === "/auth/start") return await startAuth(url, env);
      if (url.pathname === "/auth/callback") return await callback(url, env);
    } catch (e) {
      return page("Lỗi", `<p>❌ ${escapeHtml(String(e))}</p>`);
    }
    return page("MM0 Connect", `<p>Worker kết nối kênh đang chạy ✅</p>
      <p>Dùng nút "Kết nối kênh" trên dashboard, hoặc mở:</p>
      <code>/auth/start?channel=BROKE&kind=youtube</code>`);
  },
};

const YT_SCOPES = [
  "https://www.googleapis.com/auth/youtube.upload",
  "https://www.googleapis.com/auth/youtube",
  "https://www.googleapis.com/auth/youtube.force-ssl",
  "https://www.googleapis.com/auth/userinfo.email",
].join(" ");
const DRIVE_SCOPES = [
  "https://www.googleapis.com/auth/drive",
  "https://www.googleapis.com/auth/userinfo.email",
].join(" ");

async function startAuth(url, env) {
  const channel = url.searchParams.get("channel");
  const kind = url.searchParams.get("kind") || "youtube";
  const idToken = url.searchParams.get("t");
  if (!channel) return page("Thiếu tham số", "<p>Thiếu ?channel=</p>");
  // Xác thực người dùng đang đăng nhập -> lấy uid (multi-tenant, chống giả mạo)
  let uid = null;
  if (idToken) {
    try { uid = await verifyIdToken(idToken, env.FIREBASE_PROJECT_ID); }
    catch (e) { return page("Lỗi xác thực", `<p>Token đăng nhập không hợp lệ (${escapeHtml(String(e))}). Đăng nhập lại dashboard rồi thử lại.</p>`); }
  }
  if (!uid) return page("Thiếu đăng nhập", "<p>Hãy bấm Kết nối từ dashboard (đã đăng nhập), không mở link trực tiếp.</p>");
  const redirect = url.origin + "/auth/callback";
  const state = b64url(new TextEncoder().encode(JSON.stringify({ channel, kind, uid })));
  const p = new URLSearchParams({
    client_id: env.YT_CLIENT_ID,
    redirect_uri: redirect,
    response_type: "code",
    scope: kind === "drive" ? DRIVE_SCOPES : YT_SCOPES,
    access_type: "offline",
    prompt: "consent",           // luôn xin refresh_token mới
    include_granted_scopes: "true",
    state,
  });
  return Response.redirect("https://accounts.google.com/o/oauth2/v2/auth?" + p.toString(), 302);
}

async function callback(url, env) {
  const code = url.searchParams.get("code");
  const stateRaw = url.searchParams.get("state");
  if (!code || !stateRaw) return page("Lỗi", "<p>Thiếu code/state.</p>");
  const { channel, kind, uid } = JSON.parse(new TextDecoder().decode(ub64url(stateRaw)));
  if (!uid) return page("Lỗi", "<p>Thiếu uid — bấm Kết nối lại từ dashboard.</p>");
  const redirect = url.origin + "/auth/callback";

  // 1) đổi code -> token
  const tok = await (await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      code, client_id: env.YT_CLIENT_ID, client_secret: env.YT_CLIENT_SECRET,
      redirect_uri: redirect, grant_type: "authorization_code",
    }),
  })).json();

  if (!tok.refresh_token) {
    return page("Chưa lấy được refresh token",
      `<p>Google không trả refresh_token (thường do đã cấp quyền trước đó).</p>
       <p>Vào <a href="https://myaccount.google.com/permissions" target="_blank">myaccount.google.com/permissions</a>
       gỡ quyền app rồi bấm Kết nối lại.</p>`);
  }

  // 2) lấy email để kiểm tra + hiển thị
  let email = "";
  try {
    const ui = await (await fetch("https://www.googleapis.com/oauth2/v2/userinfo",
      { headers: { Authorization: `Bearer ${tok.access_token}` } })).json();
    email = ui.email || "";
  } catch (_) {}

  if (env.ALLOW_EMAIL && email && email !== env.ALLOW_EMAIL) {
    return page("Không được phép", `<p>Email ${escapeHtml(email)} không nằm trong danh sách cho phép.</p>`);
  }

  // 3) lưu token vào Firestore
  const at = await saAccessToken(env);
  const base = {
    channel, kind, email, owner: uid,
    client_id: env.YT_CLIENT_ID, client_secret: env.YT_CLIENT_SECRET,
    refresh_token: tok.refresh_token, connected_at: new Date().toISOString(),
  };

  if (kind === "drive") {
    // tạo/tìm folder kho "MM0-STORE" trong tài khoản Drive này
    const root = await ensureDriveFolder(tok.access_token, "MM0-STORE");
    await fsPatch(env, at, `connections/${uid}__${channel}__drive`, { ...base, root });
    await fsPatch(env, at, `storage_accounts/${uid}__${channel}`,
      { name: channel, owner: uid, email, connected_at: new Date().toISOString() },
      ["name", "owner", "email", "connected_at"]);
  } else {
    await fsPatch(env, at, `connections/${uid}__${channel}__youtube`, base);
    await fsPatch(env, at, `channels/${uid}__${channel}`,
      { channel, owner: uid, yt_ok: true, yt_checked_at: new Date().toISOString() },
      ["channel", "owner", "yt_ok", "yt_checked_at"]);
  }

  return page("Kết nối thành công 🎉",
    `<p>✅ Đã kết nối <b>${escapeHtml(channel)}</b> (${kind})${email ? " · " + escapeHtml(email) : ""}.</p>
     <p>Token đã lưu an toàn. Anh có thể đóng tab này và quay lại dashboard.</p>`);
}

async function ensureDriveFolder(accessToken, name) {
  const q = encodeURIComponent(`name='${name}' and mimeType='application/vnd.google-apps.folder' and trashed=false`);
  const list = await (await fetch(`https://www.googleapis.com/drive/v3/files?q=${q}&fields=files(id)`,
    { headers: { Authorization: `Bearer ${accessToken}` } })).json();
  if (list.files && list.files.length) return list.files[0].id;
  const created = await (await fetch("https://www.googleapis.com/drive/v3/files?fields=id",
    { method: "POST", headers: { Authorization: `Bearer ${accessToken}`, "content-type": "application/json" },
      body: JSON.stringify({ name, mimeType: "application/vnd.google-apps.folder" }) })).json();
  return created.id;
}

/* ---------- Firestore REST bằng service account (JWT RS256) ---------- */
async function saAccessToken(env) {
  const now = Math.floor(Date.now() / 1000);
  const claim = {
    iss: env.SA_CLIENT_EMAIL,
    scope: "https://www.googleapis.com/auth/datastore",
    aud: "https://oauth2.googleapis.com/token",
    iat: now, exp: now + 3600,
  };
  const header = { alg: "RS256", typ: "JWT" };
  const data = b64url(enc(JSON.stringify(header))) + "." + b64url(enc(JSON.stringify(claim)));
  const key = await importPkcs8(env.SA_PRIVATE_KEY);
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, enc(data));
  const jwt = data + "." + b64url(new Uint8Array(sig));
  const r = await (await fetch("https://oauth2.googleapis.com/token", {
    method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer", assertion: jwt }),
  })).json();
  if (!r.access_token) throw new Error("SA token fail: " + JSON.stringify(r));
  return r.access_token;
}

async function importPkcs8(pem) {
  const body = pem.replace(/-----[^-]+-----/g, "").replace(/\s+/g, "");
  const bin = Uint8Array.from(atob(body), (c) => c.charCodeAt(0));
  return crypto.subtle.importKey("pkcs8", bin.buffer,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"]);
}

async function fsPatch(env, accessToken, path, fields, mask) {
  const base = `https://firestore.googleapis.com/v1/projects/${env.FIREBASE_PROJECT_ID}/databases/(default)/documents/${path}`;
  const q = (mask || Object.keys(fields)).map((k) => `updateMask.fieldPaths=${encodeURIComponent(k)}`).join("&");
  const body = { fields: Object.fromEntries(Object.entries(fields).map(([k, v]) => [k, fsVal(v)])) };
  const res = await fetch(base + "?" + q, {
    method: "PATCH",
    headers: { Authorization: `Bearer ${accessToken}`, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error("Firestore write fail: " + res.status + " " + (await res.text()));
}

function fsVal(v) {
  if (typeof v === "boolean") return { booleanValue: v };
  if (typeof v === "number") return { integerValue: String(v) };
  return { stringValue: String(v) };
}

/* ---------- Xác thực Firebase ID token (RS256 + JWKS) -> uid ---------- */
async function verifyIdToken(token, projectId) {
  const parts = String(token).split(".");
  if (parts.length !== 3) throw new Error("format");
  const header = JSON.parse(new TextDecoder().decode(ub64url(parts[0])));
  const payload = JSON.parse(new TextDecoder().decode(ub64url(parts[1])));
  const now = Math.floor(Date.now() / 1000);
  if (payload.aud !== projectId) throw new Error("aud");
  if (payload.iss !== "https://securetoken.google.com/" + projectId) throw new Error("iss");
  if (!payload.exp || payload.exp < now) throw new Error("expired");
  if (!payload.sub) throw new Error("sub");
  const jwks = await (await fetch(
    "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com")).json();
  const jwk = (jwks.keys || []).find((k) => k.kid === header.kid);
  if (!jwk) throw new Error("kid");
  const key = await crypto.subtle.importKey("jwk", jwk,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  const signed = new TextEncoder().encode(parts[0] + "." + parts[1]);
  const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, ub64url(parts[2]), signed);
  if (!ok) throw new Error("signature");
  return payload.sub;
}

/* ---------- utils ---------- */
const enc = (s) => (s instanceof Uint8Array ? s : new TextEncoder().encode(s));
function b64url(bytes) {
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let s = btoa(String.fromCharCode(...b));
  return s.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function ub64url(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
}
function escapeHtml(s) { return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
function page(title, body) {
  return new Response(
    `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
     <title>${escapeHtml(title)}</title>
     <div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:520px;margin:60px auto;
       padding:32px;border:1px solid #e5e7eb;border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.06)">
       <h2 style="margin-top:0">${escapeHtml(title)}</h2>${body}</div>`,
    { headers: { "content-type": "text/html; charset=utf-8" } });
}
