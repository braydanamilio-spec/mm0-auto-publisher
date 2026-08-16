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
      if (url.pathname === "/api/channel-videos") return corsResp(await apiChannelVideos(request, url, env));
      if (url.pathname === "/api/video-update") return corsResp(await apiVideoUpdate(request, url, env));
      if (url.pathname === "/api/video-thumbnail") return corsResp(await apiVideoThumbnail(request, url, env));
      if (url.pathname === "/api/video-captions") return corsResp(await apiVideoCaptions(request, url, env));
      if (url.pathname === "/api/video-delete") return corsResp(await apiVideoDelete(request, url, env));
      if (url.pathname === "/api/social-posts") return corsResp(await apiSocialPosts(request, url, env));
      if (url.pathname === "/api/social-update") return corsResp(await apiSocialUpdate(request, url, env));
      if (url.pathname === "/api/social-delete") return corsResp(await apiSocialDelete(request, url, env));
      if (url.pathname === "/api/social-insights") return corsResp(await apiSocialInsights(request, url, env));
      if (url.pathname === "/api/social-comments") return corsResp(await apiSocialComments(request, url, env));
      if (url.pathname === "/api/social-comment-action") return corsResp(await apiSocialCommentAction(request, url, env));
      if (url.pathname === "/api/caption-add") return corsResp(await apiCaptionAdd(request, url, env));
      if (url.pathname === "/api/caption-delete") return corsResp(await apiCaptionDelete(request, url, env));
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

// GET /api/channel-videos?channel=&t=&max=  -> TOÀN BỘ video đã đăng trên kênh (như YouTube Studio)
async function apiChannelVideos(request, url, env) {
  const ctx = await authCtx(request, url, env);
  const max = Math.min(300, Math.max(1, Number(ctx.g("max") || 100) || 100));
  const ci = await ytGet("channels?part=contentDetails,statistics,snippet&mine=true", ctx.yat);
  const c0 = (ci.items || [])[0] || {};
  const chStat = {
    id: c0.id || "", title: (c0.snippet || {}).title || "",
    thumb: (((c0.snippet || {}).thumbnails || {}).default || {}).url || "",
    subscribers: +(((c0.statistics || {}).subscriberCount) || 0),
    totalViews: +(((c0.statistics || {}).viewCount) || 0),
    videoCount: +(((c0.statistics || {}).videoCount) || 0),
  };
  const pl = (((c0.contentDetails || {}).relatedPlaylists) || {}).uploads;
  if (!pl) return json({ ok: true, channel: chStat, items: [] });
  let ids = [], pageToken = "";
  while (ids.length < max) {
    const pi = await ytGet(`playlistItems?part=contentDetails&playlistId=${pl}&maxResults=50${pageToken ? `&pageToken=${pageToken}` : ""}`, ctx.yat);
    (pi.items || []).forEach((it) => ids.push(it.contentDetails.videoId));
    pageToken = pi.nextPageToken;
    if (!pageToken) break;
  }
  ids = ids.slice(0, max);
  const out = [];
  for (let i = 0; i < ids.length; i += 50) {
    const vr = await ytGet(`videos?part=snippet,statistics,contentDetails,status&id=${ids.slice(i, i + 50).join(",")}`, ctx.yat);
    (vr.items || []).forEach((v) => {
      const d = v.contentDetails.duration || "";
      const m = d.match(/PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?/);
      const secs = m ? ((+m[1] || 0) * 3600 + (+m[2] || 0) * 60 + (+m[3] || 0)) : 0;
      out.push({
        id: v.id, title: v.snippet.title, publishedAt: v.snippet.publishedAt,
        description: v.snippet.description || "", tags: v.snippet.tags || [],
        thumb: (v.snippet.thumbnails.medium || v.snippet.thumbnails.default || {}).url,
        views: +(v.statistics.viewCount || 0), likes: +(v.statistics.likeCount || 0),
        comments: +(v.statistics.commentCount || 0), duration: secs,
        type: secs > 0 && secs <= 60 ? "short" : "long",
        privacy: (v.status || {}).privacyStatus || "",
        madeForKids: (v.status || {}).selfDeclaredMadeForKids != null
          ? !!(v.status.selfDeclaredMadeForKids) : !!((v.status || {}).madeForKids),
        categoryId: (v.snippet || {}).categoryId || "",
        defaultLanguage: (v.snippet || {}).defaultLanguage || "",
      });
    });
  }
  out.sort((a, b) => (b.publishedAt || "").localeCompare(a.publishedAt || ""));
  return json({ ok: true, channel: chStat, count: out.length, items: out });
}

// POST /api/video-update {channel,id,title,description,tags[],privacy} -> sửa metadata video thật (như Studio)
async function apiVideoUpdate(request, url, env) {
  const ctx = await authCtx(request, url, env);
  const id = ctx.g("id"); if (!id) throw new Error("Thiếu id video.");
  const b = ctx.body || {};
  // Lấy snippet hiện tại (API bắt buộc gửi kèm categoryId + title khi update)
  const cur = await ytGet(`videos?part=snippet,status&id=${id}`, ctx.yat);
  const v = (cur.items || [])[0]; if (!v) throw new Error("Không tìm thấy video trên kênh.");
  const sn = v.snippet || {}, stt = v.status || {};
  const snippet = {
    categoryId: sn.categoryId || "22",
    title: (b.title != null ? String(b.title) : (sn.title || "")).slice(0, 100),
    description: b.description != null ? String(b.description).slice(0, 5000) : (sn.description || ""),
    tags: Array.isArray(b.tags) ? b.tags.slice(0, 60) : (sn.tags || []),
    defaultLanguage: b.defaultLanguage != null ? (b.defaultLanguage || undefined) : sn.defaultLanguage,
  };
  const status = {
    privacyStatus: b.privacy || stt.privacyStatus || "public",
    selfDeclaredMadeForKids: b.madeForKids != null ? !!b.madeForKids
      : (stt.selfDeclaredMadeForKids != null ? stt.selfDeclaredMadeForKids : !!stt.madeForKids),
  };
  try {
    await ytSend("PUT", "videos?part=snippet,status", ctx.yat, { id, snippet, status });
  } catch (e) {
    if (/insufficient|forbidden|scope|403/i.test(String(e.message)))
      throw new Error("Cần KẾT NỐI LẠI kênh để cấp quyền sửa/xoá video. Vào My Channels → 🔗 Kết nối lại.");
    throw e;
  }
  return json({ ok: true });
}

// POST /api/video-thumbnail {channel,id,image=dataURL} -> đặt thumbnail tùy chỉnh
async function apiVideoThumbnail(request, url, env) {
  const ctx = await authCtx(request, url, env);
  const id = ctx.g("id"); const dataUrl = (ctx.body || {}).image;
  if (!id || !dataUrl) throw new Error("Thiếu id hoặc ảnh.");
  const m = /^data:(image\/[\w.+-]+);base64,(.+)$/.exec(dataUrl);
  if (!m) throw new Error("Ảnh không hợp lệ (cần JPG/PNG).");
  const bin = atob(m[2]); const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  if (bytes.length > 2 * 1024 * 1024) throw new Error("Ảnh quá 2MB — chọn ảnh nhỏ hơn.");
  const r = await fetch(`https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId=${encodeURIComponent(id)}`,
    { method: "POST", headers: { Authorization: `Bearer ${ctx.yat}`, "content-type": m[1] }, body: bytes });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (r.status === 403) throw new Error("Kênh cần XÁC MINH số điện thoại để đặt thumbnail tùy chỉnh (hoặc Kết nối lại để cấp quyền).");
    throw new Error((j.error && j.error.message) || ("Thumbnail " + r.status));
  }
  const it = (j.items || [])[0] || {};
  return json({ ok: true, thumb: (it.medium || it.high || it.default || {}).url || "" });
}

// GET /api/video-captions?channel=&id= -> danh sách phụ đề hiện có của video
async function apiVideoCaptions(request, url, env) {
  const ctx = await authCtx(request, url, env);
  const id = ctx.g("id"); if (!id) throw new Error("Thiếu id video.");
  const r = await ytGet(`captions?part=snippet&videoId=${encodeURIComponent(id)}`, ctx.yat);
  const items = (r.items || []).map((c) => ({
    id: c.id, language: (c.snippet || {}).language || "",
    name: (c.snippet || {}).name || "", trackKind: (c.snippet || {}).trackKind || "",
    auto: (c.snippet || {}).trackKind === "ASR",
    lastUpdated: (c.snippet || {}).lastUpdated || "",
  }));
  return json({ ok: true, items });
}

// POST /api/video-delete {channel,id} -> XOÁ video thật khỏi YouTube (không thể hoàn tác)
async function apiVideoDelete(request, url, env) {
  const ctx = await authCtx(request, url, env);
  const id = ctx.g("id"); if (!id) throw new Error("Thiếu id video.");
  const r = await fetch(`https://www.googleapis.com/youtube/v3/videos?id=${encodeURIComponent(id)}`,
    { method: "DELETE", headers: { Authorization: `Bearer ${ctx.yat}` } });
  if (r.status !== 204) {
    const j = await r.json().catch(() => ({}));
    if (r.status === 403) throw new Error("Cần KẾT NỐI LẠI kênh để cấp quyền xoá video. Vào My Channels → 🔗 Kết nối lại.");
    throw new Error((j.error && j.error.message) || ("Xoá lỗi " + r.status));
  }
  return json({ ok: true });
}

/* ================= PHỤ ĐỀ: thêm / xoá (YouTube) ================= */
// POST /api/caption-add {channel,id,language,name,content} -> captions.insert (content = text .srt/.vtt hoặc dataURL)
async function apiCaptionAdd(request, url, env) {
  const ctx = await authCtx(request, url, env);
  const b = ctx.body || {}, id = b.id, lang = b.language, name = b.name || "";
  if (!id || !lang || !b.content) throw new Error("Thiếu video / ngôn ngữ / nội dung phụ đề.");
  let text = String(b.content);
  const m = /^data:[^;,]*;base64,(.+)$/.exec(text); if (m) text = decodeURIComponent(escape(atob(m[1])));
  const boundary = "mm0cap" + Math.random().toString(36).slice(2);
  const meta = JSON.stringify({ snippet: { videoId: id, language: lang, name, isDraft: false } });
  const body = `--${boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n${meta}\r\n` +
    `--${boundary}\r\nContent-Type: application/octet-stream\r\n\r\n${text}\r\n--${boundary}--`;
  const r = await fetch("https://www.googleapis.com/upload/youtube/v3/captions?part=snippet", {
    method: "POST", headers: { Authorization: `Bearer ${ctx.yat}`, "content-type": `multipart/related; boundary=${boundary}` }, body });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (r.status === 403) throw new Error("Cần KẾT NỐI LẠI kênh để cấp quyền phụ đề (force-ssl), hoặc kênh chưa bật.");
    throw new Error((j.error && j.error.message) || ("Thêm phụ đề lỗi " + r.status));
  }
  return json({ ok: true });
}
// POST /api/caption-delete {channel,id(caption id)}
async function apiCaptionDelete(request, url, env) {
  const ctx = await authCtx(request, url, env);
  const id = ctx.g("id"); if (!id) throw new Error("Thiếu id phụ đề.");
  const r = await fetch(`https://www.googleapis.com/youtube/v3/captions?id=${encodeURIComponent(id)}`,
    { method: "DELETE", headers: { Authorization: `Bearer ${ctx.yat}` } });
  if (r.status !== 204) { const j = await r.json().catch(() => ({})); throw new Error((j.error && j.error.message) || ("Xoá phụ đề lỗi " + r.status)); }
  return json({ ok: true });
}

/* ================= FACEBOOK + INSTAGRAM (tách hẳn YouTube) ================= *
 * Auth theo connections/{uid}__{channel}__facebook (page_token, page_id, ig_user_id).
 * FB: liệt kê/sửa/xoá video + bình luận. IG: liệt kê media + bình luận (Meta KHÔNG cho sửa/xoá media). */
async function fbAuthCtx(request, url, env) {
  const body = request.method === "POST" ? await request.json().catch(() => ({})) : {};
  const g = (k) => body[k] != null ? body[k] : url.searchParams.get(k);
  const t = g("t"), channel = g("channel"), platform = (g("platform") || "fb").toLowerCase();
  if (!t) throw new Error("Thiếu token đăng nhập.");
  if (!channel) throw new Error("Thiếu trang (channel).");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  const at = await saAccessToken(env);
  const conn = await fsGet(env, at, `connections/${uid}__${channel}__facebook`);
  if (!conn || !conn.page_token) throw new Error("Trang chưa kết nối Facebook.");
  if (platform === "ig" && !conn.ig_user_id) throw new Error("Trang này chưa liên kết Instagram Business.");
  return { body, g, uid, at, channel, platform, page_id: conn.page_id, page_token: conn.page_token,
    ig_user_id: conn.ig_user_id, ig_username: conn.ig_username, page_name: conn.page_name };
}
const GRAPH = "https://graph.facebook.com/v19.0/";
async function fbGet(pq, token) {
  const r = await fetch(GRAPH + pq + (pq.includes("?") ? "&" : "?") + "access_token=" + encodeURIComponent(token));
  const j = await r.json(); if (!r.ok || j.error) throw new Error((j.error && j.error.message) || ("Graph " + r.status)); return j;
}
async function fbSend(method, pq, token, params) {
  const form = new URLSearchParams(); for (const k in (params || {})) form.set(k, params[k]); form.set("access_token", token);
  const r = await fetch(GRAPH + pq, { method, body: form });
  const j = await r.json().catch(() => ({})); if (!r.ok || j.error) throw new Error((j.error && j.error.message) || ("Graph " + r.status)); return j;
}
// GET /api/social-posts?channel=&platform=fb|ig&max=
async function apiSocialPosts(request, url, env) {
  const ctx = await fbAuthCtx(request, url, env);
  const max = Math.min(100, Math.max(1, Number(ctx.g("max") || 50) || 50));
  if (ctx.platform === "ig") {
    const r = await fbGet(`${ctx.ig_user_id}/media?fields=id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count&limit=${max}`, ctx.page_token);
    const items = (r.data || []).map((m) => ({
      id: m.id, title: ((m.caption || "").split("\n")[0] || "").slice(0, 120) || "(không có caption)",
      description: m.caption || "", type: m.media_product_type === "REELS" ? "reel" : (m.media_type || "").toLowerCase(),
      thumb: m.thumbnail_url || m.media_url || "", url: m.permalink || "", publishedAt: m.timestamp || "",
      views: 0, likes: +(m.like_count || 0), comments: +(m.comments_count || 0), canEdit: false, canDelete: false,
    }));
    return json({ ok: true, platform: "ig", account: ctx.ig_username || "", items });
  }
  // Lấy TẤT CẢ bài đăng (video + ảnh + text + reel) để "tổng bài" đúng; fallback /videos nếu thiếu quyền.
  let data = [];
  try {
    const r = await fbGet(`${ctx.page_id}/published_posts?fields=id,message,story,created_time,full_picture,permalink_url,status_type,attachments{media_type,type,title,description},shares,likes.summary(true),comments.summary(true)&limit=${max}`, ctx.page_token);
    data = (r.data || []).map((p) => {
      const att = ((p.attachments && p.attachments.data) || [])[0] || {};
      const mt = String(att.media_type || att.type || p.status_type || "").toLowerCase();
      const title = ((p.message || att.title || p.story || "").split("\n")[0] || "").slice(0, 120) || "(bài viết)";
      return {
        id: p.id, title, description: p.message || "",
        type: mt.includes("video") ? "video" : (mt.includes("photo") || mt.includes("image")) ? "photo" : (mt || "post"),
        thumb: p.full_picture || "", url: p.permalink_url || "", publishedAt: p.created_time || "",
        views: 0, likes: +((p.likes && p.likes.summary && p.likes.summary.total_count) || 0),
        comments: +((p.comments && p.comments.summary && p.comments.summary.total_count) || 0),
        shares: +((p.shares && p.shares.count) || 0), canEdit: true, canDelete: true,
      };
    });
  } catch (e) {
    // Fallback: chỉ video upload
    const r = await fbGet(`${ctx.page_id}/videos?fields=id,title,description,created_time,permalink_url,picture,length,likes.summary(true),comments.summary(true),views&limit=${max}`, ctx.page_token);
    data = (r.data || []).map((v) => {
      const pu = v.permalink_url || "";
      return {
        id: v.id, title: v.title || ((v.description || "").split("\n")[0] || "").slice(0, 120) || "(video)",
        description: v.description || "", type: "video",
        thumb: v.picture || "", url: pu ? (pu.startsWith("http") ? pu : "https://www.facebook.com" + pu) : "",
        publishedAt: v.created_time || "", views: +(v.views || 0),
        likes: +((v.likes && v.likes.summary && v.likes.summary.total_count) || 0),
        comments: +((v.comments && v.comments.summary && v.comments.summary.total_count) || 0),
        canEdit: true, canDelete: true,
      };
    });
  }
  return json({ ok: true, platform: "fb", account: ctx.page_name || "", items: data });
}
// POST /api/social-update {channel,platform,id,title,description}  (chỉ FB)
async function apiSocialUpdate(request, url, env) {
  const ctx = await fbAuthCtx(request, url, env);
  if (ctx.platform === "ig") throw new Error("Instagram KHÔNG cho sửa bài đã đăng (giới hạn của Meta).");
  const b = ctx.body || {}, id = b.id; if (!id) throw new Error("Thiếu id.");
  const p = {}; if (b.title != null) p.title = String(b.title).slice(0, 255); if (b.description != null) p.description = String(b.description);
  await fbSend("POST", `${id}`, ctx.page_token, p);
  return json({ ok: true });
}
// POST /api/social-delete {channel,platform,id}  (chỉ FB)
async function apiSocialDelete(request, url, env) {
  const ctx = await fbAuthCtx(request, url, env);
  if (ctx.platform === "ig") throw new Error("Instagram KHÔNG cho xoá media qua API (giới hạn của Meta).");
  const id = ctx.g("id"); if (!id) throw new Error("Thiếu id.");
  await fbSend("DELETE", `${id}`, ctx.page_token, {});
  return json({ ok: true });
}
// GET /api/social-insights?channel=&platform=  -> số liệu trang/tài khoản
async function apiSocialInsights(request, url, env) {
  const ctx = await fbAuthCtx(request, url, env);
  if (ctx.platform === "ig") {
    const r = await fbGet(`${ctx.ig_user_id}?fields=username,name,followers_count,media_count,profile_picture_url`, ctx.page_token);
    return json({ ok: true, platform: "ig", username: r.username || "", name: r.name || "",
      avatar: r.profile_picture_url || "", followers: +(r.followers_count || 0), posts: +(r.media_count || 0) });
  }
  const r = await fbGet(`${ctx.page_id}?fields=name,fan_count,followers_count`, ctx.page_token);
  return json({ ok: true, platform: "fb", name: r.name || "", followers: +(r.followers_count || r.fan_count || 0), fans: +(r.fan_count || 0) });
}
// GET /api/social-comments?channel=&platform=&mediaId=&max=
async function apiSocialComments(request, url, env) {
  const ctx = await fbAuthCtx(request, url, env);
  const mediaId = ctx.g("mediaId"); if (!mediaId) throw new Error("Thiếu id bài viết.");
  const max = Math.min(50, Math.max(1, Number(ctx.g("max") || 25) || 25));
  if (ctx.platform === "ig") {
    const r = await fbGet(`${mediaId}/comments?fields=id,username,text,timestamp,like_count&limit=${max}`, ctx.page_token);
    return json({ ok: true, items: (r.data || []).map((c) => ({ id: c.id, author: c.username || "", text: c.text || "", publishedAt: c.timestamp || "", likes: +(c.like_count || 0) })) });
  }
  const r = await fbGet(`${mediaId}/comments?fields=id,from,message,created_time,like_count&order=reverse_chronological&limit=${max}`, ctx.page_token);
  return json({ ok: true, items: (r.data || []).map((c) => ({ id: c.id, author: (c.from && c.from.name) || "", text: c.message || "", publishedAt: c.created_time || "", likes: +(c.like_count || 0) })) });
}
// POST /api/social-comment-action {channel,platform,action(reply|hide|delete),id,text}
async function apiSocialCommentAction(request, url, env) {
  const ctx = await fbAuthCtx(request, url, env);
  const b = ctx.body || {}, action = b.action, id = b.id; if (!id || !action) throw new Error("Thiếu id / hành động.");
  if (action === "delete") { await fbSend("DELETE", `${id}`, ctx.page_token, {}); return json({ ok: true }); }
  if (action === "hide") { await fbSend("POST", `${id}`, ctx.page_token, ctx.platform === "ig" ? { hide: "true" } : { is_hidden: "true" }); return json({ ok: true }); }
  if (action === "reply") {
    const text = (b.text || "").trim(); if (!text) throw new Error("Thiếu nội dung trả lời.");
    if (ctx.platform === "ig") await fbSend("POST", `${id}/replies`, ctx.page_token, { message: text });
    else await fbSend("POST", `${id}/comments`, ctx.page_token, { message: text });
    return json({ ok: true });
  }
  throw new Error("Hành động không hợp lệ.");
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
    else fp.set("scope", "pages_show_list,pages_manage_posts,pages_read_engagement,pages_manage_engagement,business_management,instagram_basic,instagram_content_publish,instagram_manage_comments,instagram_manage_insights,read_insights");
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
  // 2b) CHỦ TÀI KHOẢN FB (uid nick fb) — để nhóm Page/IG cùng 1 người tạo (như nhóm YouTube theo Gmail)
  let fb_owner_id = "", fb_owner_name = "";
  try {
    const me = await (await fetch(`https://graph.facebook.com/v19.0/me?fields=id,name&access_token=${userTok}`)).json();
    if (me && me.id) { fb_owner_id = me.id; fb_owner_name = me.name || ""; }
    // Login for Business đôi khi /me rỗng -> thử businesses
    if (!fb_owner_id) {
      const bz = await (await fetch(`https://graph.facebook.com/v19.0/me/businesses?fields=id,name&access_token=${userTok}`)).json();
      const b = (bz.data || [])[0];
      if (b && b.id) { fb_owner_id = "biz_" + b.id; fb_owner_name = b.name || "Facebook Business"; }
    }
  } catch (_) {}
  // 3) danh sách Page + page token (page token không hết hạn khi user token dài hạn)
  const pages = await (await fetch(`https://graph.facebook.com/v19.0/me/accounts?fields=id,name,access_token&access_token=${userTok}`)).json();
  const list = pages.data || [];
  // BẢO ĐẢM luôn có khoá nhóm: nếu /me + business đều rỗng -> dùng khoá-lô theo Page đầu (mọi Page connect chung 1 lần vẫn gom nhóm)
  if (!fb_owner_id && list.length) { fb_owner_id = "fbgrp_" + list[0].id; fb_owner_name = fb_owner_name || "Nhóm kết nối"; }
  if (!list.length) return page("Không tìm thấy Page",
    `<p>Tài khoản Facebook này chưa quản lý Page nào, hoặc chưa cấp quyền Page.</p>
     <p>${escapeHtml((pages.error && pages.error.message) || "")}</p>`, "facebook");
  const at = await saAccessToken(env);
  let igCount = 0;
  for (const pg of list) {
    const slug = slugLabel(pg.name) || ("PAGE_" + String(pg.id).slice(-6));
    // Lấy Instagram Business account liên kết với Page (để đăng IG luôn) + avatar
    let ig_user_id = "", ig_username = "", ig_avatar = "", ig_name = "";
    try {
      const igr = await (await fetch(
        `https://graph.facebook.com/v19.0/${pg.id}?fields=instagram_business_account&access_token=${pg.access_token}`)).json();
      const ig = igr.instagram_business_account;
      if (ig && ig.id) {
        ig_user_id = ig.id; igCount++;
        // Gọi TRỰC TIẾP node IG để lấy chắc chắn tên + ảnh profile
        try {
          const prof = await (await fetch(
            `https://graph.facebook.com/v19.0/${ig.id}?fields=username,name,profile_picture_url&access_token=${pg.access_token}`)).json();
          ig_username = prof.username || ""; ig_name = prof.name || ""; ig_avatar = prof.profile_picture_url || "";
        } catch (_) {}
      }
    } catch (_) {}
    await fsPatch(env, at, `connections/${uid}__${slug}__facebook`,
      { channel: slug, kind: "facebook", owner: uid, page_id: pg.id, page_name: pg.name,
        page_token: pg.access_token, ig_user_id, ig_username, ig_name, ig_avatar,
        fb_owner_id, fb_owner_name, connected_at: new Date().toISOString() });
    await fsPatch(env, at, `fb_pages/${uid}__${slug}`,
      { name: slug, owner: uid, page_id: pg.id, page_name: pg.name,
        ig_user_id, ig_username, ig_name, ig_avatar, fb_owner_id, fb_owner_name, fb_ok: true, connected_at: new Date().toISOString() },
      ["name", "owner", "page_id", "page_name", "ig_user_id", "ig_username", "ig_name", "ig_avatar", "fb_owner_id", "fb_owner_name", "fb_ok", "connected_at"]);
  }
  return page("Kết nối Facebook thành công 🎉",
    `<p>✅ Đã kết nối <b>${list.length}</b> Page${igCount ? ` · <b>${igCount}</b> có Instagram` : ""}: ${list.map(p => escapeHtml(p.name)).join(", ")}.</p>
     <p>👥 Nhóm quản lý (tự nhận): <b>${escapeHtml(fb_owner_name || "—")}</b>${fb_owner_id ? ` <code>UID ${escapeHtml(String(fb_owner_id))}</code>` : ""} — các Page trên đã tự gom về nhóm này.</p>
     <p>Quản lý ở tab <b>Mạng xã hội</b> trên dashboard.</p>`, "facebook");
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
