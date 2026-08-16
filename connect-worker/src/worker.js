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
    // Preflight CORS cho các API (dashboard gọi cross-origin)
    if (request.method === "OPTIONS") return corsResp(new Response(null, { status: 204 }));
    try {
      if (url.pathname === "/auth/start") return await startAuth(url, env);
      if (url.pathname === "/auth/callback") return await callback(url, env);
      // ---- API JSON (branding + comment/like) — có CORS ----
      if (url.pathname === "/api/branding") return corsResp(await apiBranding(request, url, env));
      if (url.pathname === "/api/comments") return corsResp(await apiComments(request, url, env));
      if (url.pathname === "/api/comment-action") return corsResp(await apiCommentAction(request, url, env));
      if (url.pathname === "/api/disconnect") return corsResp(await apiDisconnect(request, url, env));
      if (url.pathname === "/api/files") return corsResp(await apiFiles(request, url, env));
      if (url.pathname === "/api/file-action") return corsResp(await apiFileAction(request, url, env));
      if (url.pathname === "/api/analytics") return corsResp(await apiAnalytics(request, url, env));
    } catch (e) {
      // API trả JSON lỗi; trang HTML trả trang lỗi
      if (url.pathname.startsWith("/api/")) return corsResp(json({ error: String(e && e.message || e) }, 400));
      return page("Lỗi", `<p>❌ ${escapeHtml(String(e))}</p>`);
    }
    return page("MM0 Connect", `<p>Worker kết nối kênh đang chạy ✅</p>
      <p>Dùng nút "Kết nối kênh" trên dashboard, hoặc mở:</p>
      <code>/auth/start?channel=BROKE&kind=youtube</code>`);
  },
};

/* ================= API: Branding + Comment/Like ================= *
 * Bảo mật: mọi request phải kèm Firebase ID token (t) -> verify -> uid.
 * Chỉ thao tác trên kênh mà uid này đã kết nối (connections/{uid}__{channel}__youtube).
 * Worker đọc refresh_token bằng service account, đổi lấy access_token, gọi YouTube API.
 * -> Dashboard KHÔNG bao giờ chạm vào token (an toàn, không lộ client-side).           */

async function authCtx(request, url, env) {
  const body = request.method === "POST" ? await request.json().catch(() => ({})) : {};
  const g = (k) => body[k] != null ? body[k] : url.searchParams.get(k);
  const t = g("t"), channel = g("channel");
  if (!t) throw new Error("Thiếu token đăng nhập.");
  if (!channel) throw new Error("Thiếu channel.");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  const at = await saAccessToken(env);
  const conn = await fsGet(env, at, `connections/${uid}__${channel}__youtube`);
  if (!conn || !conn.refresh_token) throw new Error("Kênh chưa kết nối YouTube.");
  const yat = await ytAccessToken(conn.client_id, conn.client_secret, conn.refresh_token);
  return { body, g, uid, channel, at, yat };
}

async function ytGet(pathQuery, yat) {
  const r = await fetch("https://www.googleapis.com/youtube/v3/" + pathQuery,
    { headers: { Authorization: `Bearer ${yat}` } });
  const j = await r.json();
  if (!r.ok) throw new Error((j.error && j.error.message) || ("YouTube " + r.status));
  return j;
}
async function ytSend(method, pathQuery, yat, payload) {
  const r = await fetch("https://www.googleapis.com/youtube/v3/" + pathQuery, {
    method, headers: { Authorization: `Bearer ${yat}`, "content-type": "application/json" },
    body: payload ? JSON.stringify(payload) : undefined,
  });
  if (r.status === 204) return {};
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((j.error && j.error.message) || ("YouTube " + r.status));
  return j;
}

// GET  /api/branding?channel=&t=   -> đọc branding hiện tại
// POST /api/branding {t,channel,title,description,keywords,country,trailer} -> cập nhật
async function apiBranding(request, url, env) {
  const ctx = await authCtx(request, url, env);
  const cur = await ytGet("channels?part=brandingSettings,snippet,statistics&mine=true", ctx.yat);
  const it = (cur.items || [])[0];
  if (!it) throw new Error("Không đọc được kênh.");
  const ch = (it.brandingSettings && it.brandingSettings.channel) || {};
  if (request.method !== "POST") {
    return json({
      ok: true, id: it.id,
      title: ch.title || (it.snippet && it.snippet.title) || "",
      description: ch.description || (it.snippet && it.snippet.description) || "",
      keywords: ch.keywords || "",
      country: ch.country || "",
      trailer: ch.unsubscribedTrailer || "",
      subscribers: Number((it.statistics && it.statistics.subscriberCount) || 0),
    });
  }
  // Cập nhật: merge lên brandingSettings.channel hiện có
  const b = ctx.body;
  const next = { ...ch };
  if (b.title != null) next.title = b.title;
  if (b.description != null) next.description = b.description;
  if (b.keywords != null) next.keywords = b.keywords;
  if (b.country != null) next.country = b.country || undefined;
  if (b.trailer != null) next.unsubscribedTrailer = b.trailer || undefined;
  await ytSend("PUT", "channels?part=brandingSettings", ctx.yat,
    { id: it.id, brandingSettings: { channel: next } });
  // đồng bộ tên/desc mới về Firestore để dashboard hiển thị
  try {
    await fsPatch(env, ctx.at, `channels/${ctx.uid}__${ctx.channel}`,
      { channel_title: next.title || "", branding_updated_at: new Date().toISOString() },
      ["channel_title", "branding_updated_at"]);
  } catch (_) {}
  return json({ ok: true, saved: next });
}

// GET /api/comments?channel=&t=&max=  -> danh sách bình luận mới nhất của kênh
async function apiComments(request, url, env) {
  const ctx = await authCtx(request, url, env);
  const info = await ytGet("channels?part=id&mine=true", ctx.yat);
  const cid = ((info.items || [])[0] || {}).id;
  if (!cid) throw new Error("Không đọc được channel id.");
  const max = Math.min(50, Number(ctx.g("max") || 20) || 20);
  let data;
  try {
    data = await ytGet(`commentThreads?part=snippet,replies&allThreadsRelatedToChannelId=${cid}&order=time&maxResults=${max}&textFormat=plainText`, ctx.yat);
  } catch (e) {
    // Một số kênh chưa bật, hoặc chưa có video -> trả rỗng thay vì lỗi
    return json({ ok: true, items: [], note: String(e.message || e) });
  }
  const items = (data.items || []).map((th) => {
    const s = th.snippet.topLevelComment.snippet;
    return {
      threadId: th.id,
      commentId: th.snippet.topLevelComment.id,
      author: s.authorDisplayName,
      authorImg: s.authorProfileImageUrl,
      text: s.textDisplay,
      likes: s.likeCount,
      publishedAt: s.publishedAt,
      videoId: s.videoId || "",
      totalReplies: th.snippet.totalReplyCount,
      canReply: th.snippet.canReply !== false,
      replies: ((th.replies && th.replies.comments) || []).map((c) => ({
        id: c.id, author: c.snippet.authorDisplayName, text: c.snippet.textDisplay,
        publishedAt: c.snippet.publishedAt,
      })),
    };
  });
  return json({ ok: true, channelId: cid, items });
}

// POST /api/comment-action {t,channel,action,id,text,videoId}
//   action: reply | delete | spam | hold | publish | reject | like-video | unlike-video
async function apiCommentAction(request, url, env) {
  const ctx = await authCtx(request, url, env);
  const { action, id, text, videoId } = ctx.body;
  if (!action) throw new Error("Thiếu action.");
  switch (action) {
    case "reply":
      if (!id || !text) throw new Error("Thiếu id/text.");
      await ytSend("POST", "comments?part=snippet", ctx.yat,
        { snippet: { parentId: id, textOriginal: text } });
      return json({ ok: true, action });
    case "delete":
      if (!id) throw new Error("Thiếu id.");
      await ytSend("DELETE", `comments?id=${encodeURIComponent(id)}`, ctx.yat);
      return json({ ok: true, action });
    case "spam":
      await ytSend("POST", `comments/markAsSpam?id=${encodeURIComponent(id)}`, ctx.yat);
      return json({ ok: true, action });
    case "hold":
    case "publish":
    case "reject": {
      const map = { hold: "heldForReview", publish: "published", reject: "rejected" };
      await ytSend("POST",
        `comments/setModerationStatus?id=${encodeURIComponent(id)}&moderationStatus=${map[action]}`,
        ctx.yat);
      return json({ ok: true, action });
    }
    case "like-video":
    case "unlike-video": {
      if (!videoId) throw new Error("Thiếu videoId.");
      const rating = action === "like-video" ? "like" : "none";
      await ytSend("POST", `videos/rate?id=${encodeURIComponent(videoId)}&rating=${rating}`, ctx.yat);
      return json({ ok: true, action });
    }
    default:
      throw new Error("action không hỗ trợ: " + action);
  }
}

// GET /api/analytics?channel=&t=&days=  -> PHÂN TÍCH TOÀN KÊNH theo kỳ (mọi video, kể cả không đăng bằng tool)
async function apiAnalytics(request, url, env) {
  const ctx = await authCtx(request, url, env);   // verify token + mint yt access token của kênh
  const days = Math.min(365, Math.max(1, Number(ctx.g("days") || 30) || 30));
  const now = new Date();
  const end = now.toISOString().slice(0, 10);
  const start = new Date(now.getTime() - days * 86400000).toISOString().slice(0, 10);
  const metrics = "views,estimatedMinutesWatched,likes,comments,subscribersGained,subscribersLost";
  async function q(extra) {
    const u = `https://youtubeanalytics.googleapis.com/v2/reports?ids=channel==MINE&startDate=${start}&endDate=${end}&metrics=${metrics}${extra}`;
    const r = await fetch(u, { headers: { Authorization: `Bearer ${ctx.yat}` } });
    const j = await r.json();
    if (!r.ok) {
      if (r.status === 403) throw new Error("Cần KẾT NỐI LẠI kênh để cấp quyền phân tích (Analytics). Bấm 'Kết nối lại YouTube'.");
      throw new Error((j.error && j.error.message) || ("Analytics " + r.status));
    }
    return j;
  }
  const daily = await q("&dimensions=day&sort=day");
  const totals = await q("");
  const top = await q("&dimensions=video&sort=-views&maxResults=15");
  // resolve tên + thumbnail cho top video
  const ids = (top.rows || []).map((r) => r[0]).filter(Boolean);
  const titles = {};
  if (ids.length) {
    try {
      const vr = await ytGet(`videos?part=snippet&id=${ids.join(",")}`, ctx.yat);
      (vr.items || []).forEach((v) => { titles[v.id] = { title: v.snippet.title, thumb: (v.snippet.thumbnails.medium || v.snippet.thumbnails.default || {}).url }; });
    } catch (_) {}
  }
  return json({ ok: true, days, start, end,
    daily: daily.rows || [], totals: (totals.rows || [[]])[0] || [],
    headers: (daily.columnHeaders || []).map((h) => h.name),
    top: (top.rows || []).map((r) => ({ id: r[0], views: r[1], title: (titles[r[0]] || {}).title || r[0], thumb: (titles[r[0]] || {}).thumb })) });
}

// POST /api/disconnect {t, channel, kind}  -> GỠ kênh/kho khỏi tài khoản quản lý
//   Xoá doc connections + channels/storage_accounts, và thu hồi (revoke) token Google.
async function apiDisconnect(request, url, env) {
  const body = request.method === "POST" ? await request.json().catch(() => ({})) : {};
  const g = (k) => body[k] != null ? body[k] : url.searchParams.get(k);
  const t = g("t"), channel = g("channel"), kind = g("kind") || "youtube";
  if (!t) throw new Error("Thiếu token đăng nhập.");
  if (!channel) throw new Error("Thiếu channel.");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  const at = await saAccessToken(env);
  // Thu hồi quyền Google (best-effort) rồi mới xoá
  try {
    const conn = await fsGet(env, at, `connections/${uid}__${channel}__${kind}`);
    if (conn && conn.refresh_token)
      await fetch("https://oauth2.googleapis.com/revoke?token=" + encodeURIComponent(conn.refresh_token), { method: "POST" });
  } catch (_) {}
  await fsDelete(env, at, `connections/${uid}__${channel}__${kind}`);
  if (kind === "youtube") await fsDelete(env, at, `channels/${uid}__${channel}`);
  if (kind === "drive") await fsDelete(env, at, `storage_accounts/${uid}__${channel}`);
  if (kind === "facebook") await fsDelete(env, at, `fb_pages/${uid}__${channel}`);
  return json({ ok: true, channel, kind });
}

// Lấy access token của 1 tài khoản KHO (Drive) thuộc uid — dùng chung cho files/file-action
async function driveCtx(env, uid, account) {
  const at = await saAccessToken(env);
  const conn = await fsGet(env, at, `connections/${uid}__${account}__drive`);
  if (!conn || !conn.refresh_token) throw new Error("Tài khoản kho chưa kết nối.");
  const dat = await ytAccessToken(conn.client_id, conn.client_secret, conn.refresh_token);
  return { conn, dat };
}

// GET /api/files?t=&account=&folder=<id?>  -> liệt kê file/thư mục con (mặc định gốc MM0-STORE)
async function apiFiles(request, url, env) {
  const t = url.searchParams.get("t"), account = url.searchParams.get("account");
  const folder = url.searchParams.get("folder");
  if (!t) throw new Error("Thiếu token đăng nhập.");
  if (!account) throw new Error("Thiếu account.");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  const { conn, dat } = await driveCtx(env, uid, account);
  // Mặc định duyệt từ My Drive (root) để thấy TOÀN BỘ dữ liệu; MM0-STORE là 1 thư mục con.
  const parent = folder || "root";
  const search = (url.searchParams.get("q") || "").trim();
  // Có 'q' -> TÌM KIẾM theo tên trên cả tài khoản; không thì liệt kê thư mục.
  const drvQ = search
    ? `name contains '${search.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}' and trashed=false`
    : `'${parent}' in parents and trashed=false`;
  const q = encodeURIComponent(drvQ);
  const fields = encodeURIComponent("files(id,name,mimeType,size,modifiedTime,webViewLink,thumbnailLink,parents)");
  const orderBy = search ? "modifiedTime desc" : "folder,name";
  const r = await fetch(`https://www.googleapis.com/drive/v3/files?q=${q}&fields=${fields}&pageSize=200&orderBy=${orderBy}`,
    { headers: { Authorization: `Bearer ${dat}` } });
  const j = await r.json();
  if (!r.ok) throw new Error((j.error && j.error.message) || ("Drive " + r.status));
  return json({ ok: true, root: conn.root, parent, files: j.files || [] });
}

// POST /api/file-action {t,account,action,fileId,newName}
//   action: rename | trash | untrash   (xoá = đưa vào THÙNG RÁC Drive, khôi phục được)
async function apiFileAction(request, url, env) {
  const body = request.method === "POST" ? await request.json().catch(() => ({})) : {};
  const { t, account, action, fileId, newName } = body;
  if (!t) throw new Error("Thiếu token đăng nhập.");
  if (!account || !fileId || !action) throw new Error("Thiếu tham số.");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  const { dat } = await driveCtx(env, uid, account);
  let patch;
  if (action === "rename") { if (!newName) throw new Error("Thiếu tên mới."); patch = { name: newName }; }
  else if (action === "trash") patch = { trashed: true };
  else if (action === "untrash") patch = { trashed: false };
  else throw new Error("action không hỗ trợ: " + action);
  const r = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}?fields=id,name,trashed`,
    { method: "PATCH", headers: { Authorization: `Bearer ${dat}`, "content-type": "application/json" },
      body: JSON.stringify(patch) });
  const j = await r.json();
  if (!r.ok) throw new Error((j.error && j.error.message) || ("Drive " + r.status));
  return json({ ok: true, ...j });
}

/* ---------- YouTube access token từ refresh_token ---------- */
async function ytAccessToken(client_id, client_secret, refresh_token) {
  const r = await (await fetch("https://oauth2.googleapis.com/token", {
    method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id, client_secret, refresh_token, grant_type: "refresh_token" }),
  })).json();
  if (!r.access_token) throw new Error("Không lấy được access token (refresh hỏng?).");
  return r.access_token;
}

const YT_SCOPES = [
  "https://www.googleapis.com/auth/youtube.upload",
  "https://www.googleapis.com/auth/youtube",
  "https://www.googleapis.com/auth/youtube.force-ssl",
  "https://www.googleapis.com/auth/yt-analytics.readonly",   // phân tích toàn kênh theo kỳ
  "https://www.googleapis.com/auth/userinfo.email",
].join(" ");
const DRIVE_SCOPES = [
  "https://www.googleapis.com/auth/drive",
  "https://www.googleapis.com/auth/userinfo.email",
].join(" ");

// Nhiều OAuth client (mỗi project = 10.000 quota/ngày) -> scale hàng trăm channel.
// Đặt secret YT_CLIENTS = JSON: [{"id":"...","secret":"..."},{...}]. Không có -> dùng client đơn.
function ytClients(env) {
  if (env.YT_CLIENTS) {
    try {
      const a = JSON.parse(env.YT_CLIENTS);
      if (Array.isArray(a) && a.length)
        return a.map((c) => ({ id: c.id || c.client_id, secret: c.secret || c.client_secret })).filter((c) => c.id && c.secret);
    } catch (_) {}
  }
  return [{ id: env.YT_CLIENT_ID, secret: env.YT_CLIENT_SECRET }];
}

// Round-robin: gán channel mới vào client kế tiếp (chia đều tải quota giữa các project).
async function nextClientIdx(env, at, count) {
  let n = 0;
  try { const d = await fsGet(env, at, "settings/yt_rr"); n = (d && d.n) || 0; } catch (_) {}
  try { await fsPatch(env, at, "settings/yt_rr", { n: n + 1 }, ["n"]); } catch (_) {}
  return n % count;
}

async function startAuth(url, env) {
  const channel = url.searchParams.get("channel") || "";  // để trống -> tự lấy tên kênh thật ở callback
  const kind = url.searchParams.get("kind") || "youtube";
  const idToken = url.searchParams.get("t");
  // Xác thực người dùng đang đăng nhập -> lấy uid (multi-tenant, chống giả mạo)
  let uid = null;
  if (idToken) {
    try { uid = await verifyIdToken(idToken, env.FIREBASE_PROJECT_ID); }
    catch (e) { return page("Lỗi xác thực", `<p>Token đăng nhập không hợp lệ (${escapeHtml(String(e))}). Đăng nhập lại dashboard rồi thử lại.</p>`); }
  }
  if (!uid) return page("Thiếu đăng nhập", "<p>Hãy bấm Kết nối từ dashboard (đã đăng nhập), không mở link trực tiếp.</p>");
  const redirect = url.origin + "/auth/callback";
  const state = b64url(new TextEncoder().encode(JSON.stringify({ channel, kind, uid })));

  // ---- FACEBOOK: OAuth riêng (không gắn Gmail) ----
  if (kind === "facebook") {
    if (!env.FB_APP_ID) return page("Chưa cấu hình Facebook App",
      `<p>Cần tạo <b>Facebook Developer App</b> và đặt secret <code>FB_APP_ID</code>, <code>FB_APP_SECRET</code> cho Worker.</p>
       <p>Xem hướng dẫn trong <code>SETUP.md</code> (mục Facebook).</p>`, "facebook");
    const fp = new URLSearchParams({
      client_id: env.FB_APP_ID, redirect_uri: redirect, response_type: "code", state,
    });
    // App "Facebook Login for Business" dùng config_id; app thường dùng scope.
    if (env.FB_CONFIG_ID) fp.set("config_id", env.FB_CONFIG_ID);
    else fp.set("scope", "pages_show_list,pages_manage_posts,pages_read_engagement,business_management,instagram_basic,instagram_content_publish");
    return Response.redirect("https://www.facebook.com/v19.0/dialog/oauth?" + fp.toString(), 302);
  }

  // Chọn OAuth client: YouTube xoay vòng nhiều project (chia quota); Drive dùng client đầu.
  const clients = ytClients(env);
  let ci = 0;
  if (kind === "youtube" && clients.length > 1) {
    const at = await saAccessToken(env);
    ci = await nextClientIdx(env, at, clients.length);
  }
  const client = clients[ci] || clients[0];
  const st2 = b64url(new TextEncoder().encode(JSON.stringify({ channel, kind, uid, ci })));
  const p = new URLSearchParams({
    client_id: client.id,
    redirect_uri: redirect,
    response_type: "code",
    scope: kind === "drive" ? DRIVE_SCOPES : YT_SCOPES,
    access_type: "offline",
    prompt: "consent",           // luôn xin refresh_token mới
    include_granted_scopes: "true",
    state: st2,
  });
  return Response.redirect("https://accounts.google.com/o/oauth2/v2/auth?" + p.toString(), 302);
}

async function callback(url, env) {
  const code = url.searchParams.get("code");
  const stateRaw = url.searchParams.get("state");
  if (!code || !stateRaw) return page("Lỗi", "<p>Thiếu code/state.</p>");
  const { channel, kind, uid, ci } = JSON.parse(new TextDecoder().decode(ub64url(stateRaw)));
  if (!uid) return page("Lỗi", "<p>Thiếu uid — bấm Kết nối lại từ dashboard.</p>");
  const redirect = url.origin + "/auth/callback";

  if (kind === "facebook") return await fbCallback(url, env, uid, code, redirect);

  // Dùng ĐÚNG OAuth client đã chọn lúc startAuth (để token gắn đúng project quota)
  const clients = ytClients(env);
  const client = clients[ci || 0] || clients[0];

  // 1) đổi code -> token
  const tok = await (await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      code, client_id: client.id, client_secret: client.secret,
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

  // KHÔNG chặn theo email tài khoản Google được nối: multi-account pool cần nối NHIỀU Gmail khác nhau
  // (mỗi acc 15GB) + nhiều kênh. Bảo mật đã có: chỉ user đã đăng nhập dashboard (ID token -> uid) mới connect,
  // token lưu dưới uid đó. ALLOW_EMAIL chỉ còn ý nghĩa giới hạn ai được LOGIN dashboard (không áp ở đây).

  // 3) lưu token vào Firestore
  const at = await saAccessToken(env);
  const base = {
    kind, email, owner: uid,
    client_id: client.id, client_secret: client.secret,
    refresh_token: tok.refresh_token, connected_at: new Date().toISOString(),
  };

  let connectedName = channel, label = channel;
  if (kind === "drive") {
    // Nhãn kho: người dùng để trống -> tự đặt theo phần đầu email
    label = channel || slugLabel((email || "").split("@")[0]) || "STORE";
    base.channel = label;
    // tạo/tìm folder kho "MM0-STORE" trong tài khoản Drive này
    const root = await ensureDriveFolder(tok.access_token, "MM0-STORE");
    // Đọc DUNG LƯỢNG THẬT của tài khoản (free 15GB hay Google One 100GB/2TB) -> dùng cả 2
    let cap_gb = 14, used = 0;
    try {
      const ab = await (await fetch(
        "https://www.googleapis.com/drive/v3/about?fields=storageQuota",
        { headers: { Authorization: `Bearer ${tok.access_token}` } })).json();
      const q = ab.storageQuota || {};
      if (q.limit) cap_gb = Math.max(1, Math.floor(Number(q.limit) / 1e9) - 1); // chừa ~1GB
      used = Number(q.usage || 0);
    } catch (_) {}
    await fsPatch(env, at, `connections/${uid}__${label}__drive`, { ...base, root, cap_gb });
    await fsPatch(env, at, `storage_accounts/${uid}__${label}`,
      { name: label, owner: uid, email, cap_gb, used, root,
        connected_at: new Date().toISOString() },
      ["name", "owner", "email", "cap_gb", "used", "root", "connected_at"]);
    connectedName = label;
  } else {
    // Lấy thông tin kênh THẬT (title/subs/id) TRƯỚC -> tự đặt nhãn theo tên kênh nếu để trống
    let info = {};
    try {
      const r = await (await fetch(
        "https://www.googleapis.com/youtube/v3/channels?part=snippet,statistics&mine=true",
        { headers: { Authorization: `Bearer ${tok.access_token}` } })).json();
      const it = (r.items || [])[0];
      if (it) info = {
        channel_title: (it.snippet && it.snippet.title) || "",
        avatar: (it.snippet && it.snippet.thumbnails && (it.snippet.thumbnails.medium || it.snippet.thumbnails.default || {}).url) || "",
        channel_id: it.id || "",
        subscribers: Number((it.statistics && it.statistics.subscriberCount) || 0),
        total_views: Number((it.statistics && it.statistics.viewCount) || 0),
        video_count: Number((it.statistics && it.statistics.videoCount) || 0),
      };
    } catch (_) {}
    // Nhãn định tuyến = do người dùng gõ, HOẶC tự tạo từ tên kênh thật, HOẶC từ channel_id
    label = channel || slugLabel(info.channel_title) || ("CH_" + String(info.channel_id || "").slice(-6)) || "CHANNEL";
    base.channel = label;
    connectedName = info.channel_title || label;
    await fsPatch(env, at, `connections/${uid}__${label}__youtube`, base);
    await fsPatch(env, at, `channels/${uid}__${label}`,
      { channel: label, owner: uid, email, yt_ok: true, yt_checked_at: new Date().toISOString(), ...info },
      ["channel", "owner", "email", "yt_ok", "yt_checked_at", ...Object.keys(info)]);
  }

  return page("Kết nối thành công 🎉",
    `<p>✅ Đã kết nối kênh <b>${escapeHtml(connectedName)}</b>${email ? " · " + escapeHtml(email) : ""}.</p>
     <p class="sub">Nhãn định tuyến nội bộ: <code>${escapeHtml(label)}</code> — dùng để đặt tên thư mục video (OUTBOX/${escapeHtml(label)}/…).</p>
     <p>Token đã lưu an toàn. Anh có thể đóng tab này và quay lại dashboard.</p>`,
    kind === "drive" ? "storage" : "connections");
}

/* ================= FACEBOOK connect (riêng, không gắn Gmail) ================= */
async function fbCallback(url, env, uid, code, redirect) {
  // 1) code -> user token ngắn hạn
  const tok = await (await fetch(`https://graph.facebook.com/v19.0/oauth/access_token?client_id=${env.FB_APP_ID}&client_secret=${env.FB_APP_SECRET}&redirect_uri=${encodeURIComponent(redirect)}&code=${encodeURIComponent(code)}`)).json();
  if (!tok.access_token) return page("Lỗi Facebook", `<p>${escapeHtml((tok.error && tok.error.message) || JSON.stringify(tok))}</p>`, "facebook");
  // 2) đổi lấy token DÀI HẠN (~60 ngày)
  const ll = await (await fetch(`https://graph.facebook.com/v19.0/oauth/access_token?grant_type=fb_exchange_token&client_id=${env.FB_APP_ID}&client_secret=${env.FB_APP_SECRET}&fb_exchange_token=${tok.access_token}`)).json();
  const userTok = ll.access_token || tok.access_token;
  // 3) danh sách Page + page token (page token không hết hạn khi user token dài hạn)
  const pages = await (await fetch(`https://graph.facebook.com/v19.0/me/accounts?fields=id,name,access_token&access_token=${userTok}`)).json();
  const list = pages.data || [];
  if (!list.length) return page("Không tìm thấy Page",
    `<p>Tài khoản Facebook này chưa quản lý Page nào, hoặc chưa cấp quyền Page.</p>
     <p>${escapeHtml((pages.error && pages.error.message) || "")}</p>`, "facebook");
  const at = await saAccessToken(env);
  let igCount = 0;
  for (const pg of list) {
    const slug = slugLabel(pg.name) || ("PAGE_" + String(pg.id).slice(-6));
    // Lấy Instagram Business account liên kết với Page (để đăng IG luôn)
    let ig_user_id = "", ig_username = "";
    try {
      const igr = await (await fetch(
        `https://graph.facebook.com/v19.0/${pg.id}?fields=instagram_business_account{id,username}&access_token=${pg.access_token}`)).json();
      const ig = igr.instagram_business_account;
      if (ig && ig.id) { ig_user_id = ig.id; ig_username = ig.username || ""; igCount++; }
    } catch (_) {}
    await fsPatch(env, at, `connections/${uid}__${slug}__facebook`,
      { channel: slug, kind: "facebook", owner: uid, page_id: pg.id, page_name: pg.name,
        page_token: pg.access_token, ig_user_id, ig_username, connected_at: new Date().toISOString() });
    await fsPatch(env, at, `fb_pages/${uid}__${slug}`,
      { name: slug, owner: uid, page_id: pg.id, page_name: pg.name,
        ig_user_id, ig_username, fb_ok: true, connected_at: new Date().toISOString() },
      ["name", "owner", "page_id", "page_name", "ig_user_id", "ig_username", "fb_ok", "connected_at"]);
  }
  return page("Kết nối Facebook thành công 🎉",
    `<p>✅ Đã kết nối <b>${list.length}</b> Page${igCount ? ` · <b>${igCount}</b> có Instagram` : ""}: ${list.map(p => escapeHtml(p.name)).join(", ")}.</p>
     <p>Quản lý ở tab <b>Facebook</b> trên dashboard.</p>`, "facebook");
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

// Đọc 1 document Firestore -> object phẳng (chỉ các kiểu ta dùng)
async function fsGet(env, accessToken, path) {
  const u = `https://firestore.googleapis.com/v1/projects/${env.FIREBASE_PROJECT_ID}/databases/(default)/documents/${path}`;
  const res = await fetch(u, { headers: { Authorization: `Bearer ${accessToken}` } });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error("Firestore read fail: " + res.status);
  const doc = await res.json();
  const out = {};
  for (const [k, v] of Object.entries(doc.fields || {})) {
    out[k] = v.stringValue != null ? v.stringValue
      : v.integerValue != null ? Number(v.integerValue)
      : v.booleanValue != null ? v.booleanValue
      : v.doubleValue != null ? v.doubleValue : null;
  }
  return out;
}

async function fsDelete(env, accessToken, path) {
  const u = `https://firestore.googleapis.com/v1/projects/${env.FIREBASE_PROJECT_ID}/databases/(default)/documents/${path}`;
  const res = await fetch(u, { method: "DELETE", headers: { Authorization: `Bearer ${accessToken}` } });
  if (!res.ok && res.status !== 404) throw new Error("Firestore delete fail: " + res.status);
}

/* ---------- JSON + CORS ---------- */
function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status, headers: { "content-type": "application/json; charset=utf-8" },
  });
}
function corsResp(res) {
  const h = new Headers(res.headers);
  h.set("Access-Control-Allow-Origin", "*");
  h.set("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  h.set("Access-Control-Allow-Headers", "content-type");
  h.set("Access-Control-Max-Age", "86400");
  return new Response(res.body, { status: res.status, headers: h });
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
// Chuẩn hoá tên kênh thật -> nhãn định tuyến an toàn (VD "Broke Money 💸" -> "BROKE_MONEY")
function slugLabel(s) {
  return String(s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 32);
}
function page(title, body, back = "connections") {
  return new Response(
    `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
     <title>${escapeHtml(title)}</title>
     <div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:520px;margin:60px auto;
       padding:32px;border:1px solid #e5e7eb;border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.06)">
       <h2 style="margin-top:0">${escapeHtml(title)}</h2>${body}
       <div style="margin-top:24px;display:flex;gap:10px;flex-wrap:wrap">
         <a href="https://mm0-auto-publisher.web.app/#${escapeHtml(back)}"
            style="background:#6c4ee6;color:#fff;text-decoration:none;padding:11px 18px;border-radius:10px;font-weight:600;font-size:14px">← Quay lại Dashboard</a>
         <button onclick="window.close()"
            style="background:#f1f1f4;color:#333;border:none;padding:11px 18px;border-radius:10px;font-weight:600;font-size:14px;cursor:pointer">Đóng tab này</button>
       </div></div>`,
    { headers: { "content-type": "text/html; charset=utf-8" } });
}
