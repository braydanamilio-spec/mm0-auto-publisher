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
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    // Preflight CORS cho các API (dashboard gọi cross-origin)
    if (request.method === "OPTIONS") return corsResp(new Response(null, { status: 204 }));
    // LINK RÚT GỌN THƯƠNG HIỆU: /go/<code> -> 302 tới link đích (giữ UTM), đếm click.
    if (url.pathname.startsWith("/go/")) return await handleGo(url, env, ctx);
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
      if (url.pathname === "/api/file-content") return corsResp(await apiFileContent(request, url, env));
      if (url.pathname === "/api/drive-stream") return await apiDriveStream(request, url, env);
      if (url.pathname === "/api/drive-thumb") return await apiDriveThumb(request, url, env);
      if (url.pathname === "/api/drive-share") return corsResp(await apiDriveShare(request, url, env));
      if (url.pathname === "/api/drive-has") return corsResp(await apiDriveHas(request, url, env));
      if (url.pathname === "/api/empty-trash") return corsResp(await apiEmptyTrash(request, url, env));
      if (url.pathname === "/api/cf-accounts") return corsResp(await apiCfAccounts(request));
      if (url.pathname === "/api/r2-setup") return corsResp(await apiR2Setup(request));
      if (url.pathname === "/api/cf-flux") return corsResp(await apiCfFlux(request));
      if (url.pathname === "/api/drive-trash") return corsResp(await apiDriveTrash(request, url, env));
      if (url.pathname === "/api/drive-usage") return corsResp(await apiDriveUsage(request, url, env));
      if (url.pathname === "/api/token-check") return corsResp(await apiTokenCheck(request, url, env));
      if (url.pathname === "/api/upload-init") return corsResp(await apiUploadInit(request, url, env));
      if (url.pathname === "/api/upload-chunk") return corsResp(await apiUploadChunk(request, url, env));
      if (url.pathname === "/api/upload-done") return corsResp(await apiUploadDone(request, url, env));
      if (url.pathname === "/api/analytics") return corsResp(await apiAnalytics(request, url, env));
      if (url.pathname === "/api/channel-videos") return corsResp(await apiChannelVideos(request, url, env));
      if (url.pathname === "/api/monetization") return corsResp(await apiMonetization(request, url, env));
      if (url.pathname === "/api/enable-apis") return corsResp(await apiEnableApis(request, url, env));
      if (url.pathname === "/api/fb-monetization") return corsResp(await apiFbMonetization(request, url, env));
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

// GET /api/monetization?channel=  -> ĐO điều kiện YPP (1K sub + 4K giờ xem/12 tháng). KHÔNG bật kiếm tiền (Google không cho API).
async function apiMonetization(request, url, env) {
  const ctx = await authCtx(request, url, env);
  const ci = await ytGet("channels?part=statistics,snippet&mine=true", ctx.yat);
  const c0 = (ci.items || [])[0] || {};
  const subs = +(((c0.statistics || {}).subscriberCount) || 0);
  const title = (c0.snippet || {}).title || "";
  let watchHours = null, err = null, errDetail = null, errReason = null, shortsViews90 = null;
  const now = new Date();
  const end = now.toISOString().slice(0, 10);
  const start = new Date(now.getTime() - 365 * 86400000).toISOString().slice(0, 10);
  try {
    const r = await fetch(`https://youtubeanalytics.googleapis.com/v2/reports?ids=channel==MINE&startDate=${start}&endDate=${end}&metrics=estimatedMinutesWatched`,
      { headers: { Authorization: `Bearer ${ctx.yat}` } });
    const j = await r.json();
    if (r.ok) { const m = j.rows && j.rows[0] && j.rows[0][0]; watchHours = Math.round((+m || 0) / 60); }
    else {
      errDetail = (j.error && j.error.message) || ("Analytics " + r.status);
      errReason = (j.error && j.error.errors && j.error.errors[0] && j.error.errors[0].reason) || (j.error && j.error.status) || "";
      const blob = (errDetail + " " + errReason).toLowerCase();
      // Phân biệt: API CHƯA BẬT trong Google Cloud (reconnect vô ích) vs thiếu quyền (reconnect fix được)
      if (r.status === 403 && (blob.includes("accessnotconfigured") || blob.includes("service_disabled")
          || blob.includes("has not been used") || blob.includes("disabled")))
        err = "api_disabled";
      else if (r.status === 403) err = "need_analytics";
      else err = "analytics_error";
    }
  } catch (e) { err = "analytics_error"; errDetail = String(e && e.message || e); }
  const subOk = subs >= 1000;
  const hoursOk = watchHours != null && watchHours >= 4000;
  return json({
    ok: true, title, channelId: c0.id || "", subscribers: subs, watchHours, subOk, hoursOk,
    eligible: subOk && hoursOk,
    subNeed: Math.max(0, 1000 - subs), hoursNeed: watchHours != null ? Math.max(0, 4000 - watchHours) : null,
    err, errDetail, errReason,
  });
}

// POST /api/enable-apis -> TỰ BẬT YouTube Analytics + Data API cho MỌI project OAuth (1 lần, theo project).
//   Cần Service Account có quyền 'Service Usage Admin' trên project OAuth. Không đủ quyền -> trả lỗi rõ,
//   user bật tay (chỉ 1 lần/project — KHÔNG phải từng channel).
async function apiEnableApis(request, url, env) {
  const t = (request.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);   // chỉ user đã đăng nhập
  if (!uid) throw new Error("Chưa đăng nhập.");
  // Project number = phần số đầu của mỗi OAuth client id (trước dấu '-').
  const projects = [...new Set(ytClients(env)
    .map((c) => (String(c.id || "").match(/^(\d+)-/) || [])[1]).filter(Boolean))];
  if (!projects.length) throw new Error("Không tìm thấy project từ OAuth client.");
  const apis = ["youtubeanalytics.googleapis.com", "youtube.googleapis.com"];
  let at;
  try { at = await saAccessToken(env, "https://www.googleapis.com/auth/cloud-platform"); }
  catch (e) { throw new Error("SA không lấy được quyền cloud-platform: " + e.message); }
  const results = [];
  for (const proj of projects) {
    for (const api of apis) {
      try {
        const r = await fetch(`https://serviceusage.googleapis.com/v1/projects/${proj}/services/${api}:enable`,
          { method: "POST", headers: { Authorization: `Bearer ${at}`, "content-type": "application/json" }, body: "{}" });
        const j = await r.json().catch(() => ({}));
        if (r.ok) results.push({ project: proj, api, ok: true });
        else results.push({ project: proj, api, ok: false, error: (j.error && j.error.message) || ("HTTP " + r.status), status: r.status });
      } catch (e) { results.push({ project: proj, api, ok: false, error: String(e && e.message || e) }); }
    }
  }
  const allOk = results.every((x) => x.ok);
  const needManual = results.some((x) => !x.ok && (x.status === 403 || x.status === 401));
  return json({ ok: allOk, projects, results, needManual });
}

// GET /api/fb-monetization?channel=&platform=fb -> ĐO tiêu chí kiếm tiền FB (In-stream/Reels).
//   LƯU Ý TRUNG THỰC: Facebook KHÔNG có API công khai trả "đủ/không đủ điều kiện" cho app thường
//   (các endpoint monetization eligibility bị khoá cho đối tác được duyệt). Ta chỉ ĐO số liệu thô
//   (followers + phút xem 60 ngày + số video) rồi so với ngưỡng Meta công bố + dẫn tới Business Suite.
async function apiFbMonetization(request, url, env) {
  const ctx = await fbAuthCtx(request, url, env);
  const p = await fbGet(`${ctx.page_id}?fields=name,fan_count,followers_count,picture.width(100).height(100){url}`, ctx.page_token);
  const followers = +(p.followers_count || p.fan_count || 0);
  let watchMin = null, videos = null, note = null;
  // Phút xem 60 ngày — metric page_video_view_time (mili-giây). Best-effort: metric có thể bị Meta gỡ.
  try {
    const since = Math.floor((Date.now() - 60 * 86400000) / 1000);
    const until = Math.floor(Date.now() / 1000);
    const ins = await fbGet(`${ctx.page_id}/insights/page_video_view_time?period=day&since=${since}&until=${until}`, ctx.page_token);
    const vals = ((ins.data || [])[0] || {}).values || [];
    const totalMs = vals.reduce((s, v) => s + (+v.value || 0), 0);
    watchMin = Math.round(totalMs / 1000 / 60);
  } catch (e) { note = "watch_metric_unavailable"; }
  // Số video (>=5 video hoạt động là 1 tiêu chí In-stream)
  try {
    const vc = await fbGet(`${ctx.page_id}/videos?fields=id&limit=25`, ctx.page_token);
    videos = (vc.data || []).length;
  } catch (e) {}
  const followOk = followers >= 5000;
  const watchOk = watchMin != null && watchMin >= 60000;
  return json({
    ok: true, platform: "fb", name: p.name || "", avatar: (((p.picture || {}).data) || {}).url || "",
    followers, followOk, followNeed: Math.max(0, 5000 - followers),
    watchMin, watchOk, watchNeed: watchMin != null ? Math.max(0, 60000 - watchMin) : null,
    videos, videosOk: videos != null ? videos >= 5 : null, note,
  });
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
  const r = await fbGet(`${ctx.page_id}?fields=name,fan_count,followers_count,picture.width(100).height(100){url}`, ctx.page_token);
  return json({ ok: true, platform: "fb", name: r.name || "", avatar: (((r.picture || {}).data) || {}).url || "",
    followers: +(r.followers_count || r.fan_count || 0), fans: +(r.fan_count || 0) });
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
let _dctxCache = {};   // CACHE {uid__account: {conn, dat, exp}} ~45' -> thumb/stream/check lặp KHỎI đọc Firestore + đổi token mỗi lần (tiết kiệm read, chống cạn quota).
async function driveCtx(env, uid, account) {
  const key = `${uid}__${account}`;
  const c = _dctxCache[key];
  if (c && c.exp > Date.now()) return { conn: c.conn, dat: c.dat };
  const at = await saAccessToken(env);
  // 23/8: Firestore project A có ngày cạn quota đọc -> fsGet ném 429 -> KHÔNG lấy được refresh_token
  // -> pipeline tưởng "không kho nào đủ chỗ" (đúng vết sự cố 180 video). Nay đọc hụt thì lấy bản sao
  // trong KV; đọc được thì ghi lại KV. KV không tính vào quota Firestore.
  const kvKey = `conn:${uid}__${account}__drive`;
  let conn = null, fsErr = null;
  try { conn = await fsGet(env, at, `connections/${uid}__${account}__drive`); }
  catch (e) { fsErr = e; }
  if (conn && conn.refresh_token) {
    if (env.MM0_CACHE) { try { await env.MM0_CACHE.put(kvKey, JSON.stringify(conn)); } catch (_) {} }
  } else if (env.MM0_CACHE) {
    try { const raw = await env.MM0_CACHE.get(kvKey); if (raw) conn = JSON.parse(raw); } catch (_) {}
  }
  if (!conn || !conn.refresh_token) throw new Error("Tài khoản kho chưa kết nối." + (fsErr ? " (" + String(fsErr).slice(0, 60) + ")" : ""));
  const dat = await ytAccessToken(conn.client_id, conn.client_secret, conn.refresh_token);
  _dctxCache[key] = { conn, dat, exp: Date.now() + 45 * 60 * 1000 };   // access token sống 1h -> cache 45' an toàn
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

// GET /api/token-check?account=<drive> HOẶC ?channel=<yt> -> kiểm tra token còn sống không (nhẹ).
async function apiTokenCheck(request, url, env) {
  const t = url.searchParams.get("t"); const account = url.searchParams.get("account"); const channel = url.searchParams.get("channel");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  if (!uid) throw new Error("Chưa đăng nhập.");
  const at = await saAccessToken(env);
  const path = account ? `connections/${uid}__${account}__drive` : `connections/${uid}__${channel}__youtube`;
  const conn = await fsGet(env, at, path);
  if (!conn || !conn.refresh_token) return json({ ok: false, healthy: false, reason: "not_connected" });
  try {
    const tok = await ytAccessToken(conn.client_id, conn.client_secret, conn.refresh_token);
    // KIỂM QUYỀN (scope) cho kho DRIVE: đủ quyền = có scope 'auth/drive' (full: upload + share link + xóa).
    let scopes = "", fullEdit = null;
    if (account) {
      try {
        const ti = await fetch("https://oauth2.googleapis.com/tokeninfo?access_token=" + encodeURIComponent(tok));
        const tj = await ti.json();
        scopes = tj.scope || "";
        fullEdit = /https:\/\/www\.googleapis\.com\/auth\/drive(\.file)?(\s|$)/.test(scopes);   // 23/8: drive.file cũng ĐỦ quyền (app chỉ đụng file mình tạo)
      } catch (_) { }
    }
    return json({ ok: true, healthy: true, scopes, fullEdit });
  } catch (e) {
    return json({ ok: true, healthy: false, invalid: !!e.tokenInvalid, reason: String(e && e.message || e) });
  }
}

// Tìm/tạo thư mục con theo tên (dùng OAuth token của kho).
async function driveChildFolder(dat, parent, name, create = true) {
  const q = encodeURIComponent(`'${parent}' in parents and name='${String(name).replace(/'/g, "\\'")}' and mimeType='application/vnd.google-apps.folder' and trashed=false`);
  const r = await fetch(`https://www.googleapis.com/drive/v3/files?q=${q}&fields=files(id)&pageSize=1`,
    { headers: { Authorization: `Bearer ${dat}` } });
  const j = await r.json();
  if (j.files && j.files[0]) return j.files[0].id;
  if (!create) return null;
  const c = await fetch("https://www.googleapis.com/drive/v3/files?fields=id",
    { method: "POST", headers: { Authorization: `Bearer ${dat}`, "content-type": "application/json" },
      body: JSON.stringify({ name, parents: [parent], mimeType: "application/vnd.google-apps.folder" }) });
  const cj = await c.json();
  if (!c.ok) throw new Error("Tạo folder lỗi: " + ((cj.error && cj.error.message) || c.status));
  return cj.id;
}

// GET /api/file-content?account=&fileId= -> đọc NỘI DUNG text 1 file Drive (UPLOAD.md/json/srt) để parse metadata.
async function apiFileContent(request, url, env) {
  const t = url.searchParams.get("t"), account = url.searchParams.get("account"), fileId = url.searchParams.get("fileId");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  if (!uid) throw new Error("Chưa đăng nhập.");
  if (!account || !fileId) throw new Error("Thiếu account/fileId.");
  const { dat } = await driveCtx(env, uid, account);
  const r = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}?alt=media`,
    { headers: { Authorization: `Bearer ${dat}` } });
  if (!r.ok) throw new Error("Đọc file lỗi " + r.status);
  const text = (await r.text()).slice(0, 80000);   // chỉ file text nhỏ (<=80KB)
  return json({ ok: true, text });
}

// GET /api/drive-stream?t=&account=&fileId= -> STREAM video từ Drive (dùng token của kho, Range để tua được).
//   Nhờ vậy XEM INLINE trên dashboard kể cả file CHƯA mở chia sẻ. Token kho KHÔNG lộ ra browser.
async function fsListStorageAccounts(env, at, uid) {
  const u = `https://firestore.googleapis.com/v1/projects/${env.FIREBASE_PROJECT_ID}/databases/(default)/documents:runQuery`;
  const body = { structuredQuery: { from: [{ collectionId: "storage_accounts" }],
    where: { fieldFilter: { field: { fieldPath: "owner" }, op: "EQUAL", value: { stringValue: uid } } } } };
  const res = await fetch(u, { method: "POST", headers: { Authorization: `Bearer ${at}`, "content-type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) return [];
  const rows = await res.json();
  const names = [];
  for (const r of (rows || [])) { const nm = r.document && r.document.fields && r.document.fields.name; if (nm && nm.stringValue) names.push(nm.stringValue); }
  return names;
}

async function apiDriveStream(request, url, env) {
  const t = url.searchParams.get("t"), account0 = url.searchParams.get("account"), fileId = url.searchParams.get("fileId");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  if (!uid) return new Response("unauthorized", { status: 401 });
  if (!fileId) return new Response("missing fileId", { status: 400 });
  let dat;
  try {
    if (account0) {                             // thử kho đã ghi + VERIFY file có ở đó không (bản ghi có thể lệch)
      try {
        const c = await driveCtx(env, uid, account0);
        const chk = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}?fields=id&supportsAllDrives=true`, { headers: { Authorization: `Bearer ${c.dat}` } });
        if (chk.ok) dat = c.dat;
      } catch (_) { }
    }
    if (!dat) {                                 // KHÔNG có / ghi lệch -> TỰ DÒ mọi kho tới khi mở được file
      const at = await saAccessToken(env);
      for (const nm of await fsListStorageAccounts(env, at, uid)) {
        try {
          const c = await driveCtx(env, uid, nm);
          const chk = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}?fields=id&supportsAllDrives=true`, { headers: { Authorization: `Bearer ${c.dat}` } });
          if (chk.ok) { dat = c.dat; break; }
        } catch (_) { }
      }
      if (!dat) return new Response("không tìm thấy file trong kho nào", { status: 404 });
    }
  } catch (e) { return new Response("drive auth error: " + (e && e.message || e), { status: 502 }); }
  const range = request.headers.get("Range");
  const r = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}?alt=media&supportsAllDrives=true`,
    { headers: { Authorization: `Bearer ${dat}`, ...(range ? { Range: range } : {}) } });
  const h = new Headers();
  ["content-type", "content-length", "content-range", "accept-ranges"].forEach(k => { const v = r.headers.get(k); if (v) h.set(k, v); });
  if (!h.get("content-type")) h.set("content-type", "video/mp4");
  h.set("accept-ranges", "bytes");
  h.set("Access-Control-Allow-Origin", "*");
  h.set("Cache-Control", "private, max-age=60");
  return new Response(r.body, { status: r.status, headers: h });   // pass-through stream (Range để tua)
}

// Tìm token của kho MỞ ĐƯỢC fileId. account0: thử trước + VERIFY (bản ghi có thể lệch); mở được -> dùng, không -> DÒ mọi kho.
async function resolveDriveDat(env, uid, account0, fileId) {
  if (account0) {
    try {
      const c = await driveCtx(env, uid, account0);
      if (!fileId) return c.dat;                // không có fileId để verify -> dùng luôn
      const chk = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}?fields=id&supportsAllDrives=true`, { headers: { Authorization: `Bearer ${c.dat}` } });
      if (chk.ok) return c.dat;                 // kho ghi sẵn ĐÚNG -> dùng
    } catch (_) { }                             // sai/lỗi -> rơi xuống DÒ mọi kho
  }
  const at = await saAccessToken(env);
  for (const nm of await fsListStorageAccounts(env, at, uid)) {
    try {
      const c = await driveCtx(env, uid, nm);
      const chk = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}?fields=id&supportsAllDrives=true`, { headers: { Authorization: `Bearer ${c.dat}` } });
      if (chk.ok) return c.dat;
    } catch (_) { }
  }
  return null;
}

// Dò kho THẬT SỰ SỞ HỮU / có quyền SỬA file (dùng khi xóa/sửa lỗi quyền vì bản ghi kho lệch).
// File thường share anyoneWithLink (đọc được từ mọi token) -> phải xét ownedByMe/canEdit, KHÔNG chỉ đọc được.
async function findFileOwnerDat(env, uid, fileId) {
  const at = await saAccessToken(env);
  for (const nm of await fsListStorageAccounts(env, at, uid)) {
    try {
      const c = await driveCtx(env, uid, nm);
      const r = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}?fields=id,ownedByMe,capabilities(canEdit)&supportsAllDrives=true`, { headers: { Authorization: `Bearer ${c.dat}` } });
      if (r.ok) { const j = await r.json(); if (j.ownedByMe || (j.capabilities && j.capabilities.canEdit)) return { dat: c.dat, name: nm }; }
    } catch (_) { }
  }
  return null;
}

// GET /api/drive-has?t=&account=&fileId= -> kho NÀY có chứa file không? (rẻ, để CLIENT dò song song, không dính giới hạn 22 kho).
async function apiDriveHas(request, url, env) {
  const t = url.searchParams.get("t"), account = url.searchParams.get("account"), fileId = url.searchParams.get("fileId");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  if (!uid) throw new Error("Chưa đăng nhập.");
  if (!account || !fileId) throw new Error("Thiếu account/fileId.");
  try {
    const { dat } = await driveCtx(env, uid, account);
    const r = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}?fields=id&supportsAllDrives=true`, { headers: { Authorization: `Bearer ${dat}` } });
    return json({ ok: true, has: r.ok });
  } catch (_) { return json({ ok: true, has: false }); }
}

// GET /api/drive-usage?t=&account= -> đọc dung lượng THẬT + GHI vào storage_accounts.used -> display cập nhật ngay (render upload không tự cập nhật số này).
async function apiDriveUsage(request, url, env) {
  const t = url.searchParams.get("t"), account = url.searchParams.get("account");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  if (!uid) throw new Error("Chưa đăng nhập.");
  if (!account) throw new Error("Thiếu account.");
  const { dat } = await driveCtx(env, uid, account);
  const r = await fetch("https://www.googleapis.com/drive/v3/about?fields=storageQuota", { headers: { Authorization: `Bearer ${dat}` } });
  const j = await r.json().catch(() => ({}));
  const q = j.storageQuota || {};
  const used = Number(q.usage || 0), limit = Number(q.limit || 0);
  try { const at = await saAccessToken(env); await fsPatch(env, at, `storage_accounts/${uid}__${account}`, { used }, ["used"]); } catch (_) { }
  return json({ ok: r.ok, used, limit });
}

// GET /api/drive-trash?t=&account= -> LIỆT KÊ file đang trong THÙNG RÁC (review trước khi xóa vĩnh viễn).
async function apiDriveTrash(request, url, env) {
  const t = url.searchParams.get("t"), account = url.searchParams.get("account");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  if (!uid) throw new Error("Chưa đăng nhập.");
  if (!account) throw new Error("Thiếu account.");
  const { dat } = await driveCtx(env, uid, account);
  const r = await fetch(`https://www.googleapis.com/drive/v3/files?q=${encodeURIComponent("trashed=true")}&fields=files(id,name,size,mimeType)&pageSize=1000&supportsAllDrives=true`,
    { headers: { Authorization: `Bearer ${dat}` } });
  const j = await r.json().catch(() => ({}));
  return json({ ok: r.ok, files: (j.files || []).map(f => ({ id: f.id, name: f.name, size: Number(f.size || 0), mime: f.mimeType })) });
}

// POST /api/empty-trash?t=&account= -> ĐỔ THÙNG RÁC kho (xóa vĩnh viễn file đã trash) -> THU HỒI dung lượng ngay.
async function apiEmptyTrash(request, url, env) {
  const t = url.searchParams.get("t"), account = url.searchParams.get("account");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  if (!uid) throw new Error("Chưa đăng nhập.");
  if (!account) throw new Error("Thiếu account.");
  const { dat } = await driveCtx(env, uid, account);
  const r = await fetch("https://www.googleapis.com/drive/v3/files/trash", { method: "DELETE", headers: { Authorization: `Bearer ${dat}` } });
  return json({ ok: r.ok, status: r.status });
}

// GET /api/drive-share?t=&fileId=[&account=] -> đặt quyền "ai có link đều XEM được" + trả link chia sẻ.
async function apiDriveShare(request, url, env) {
  const t = url.searchParams.get("t"), account0 = url.searchParams.get("account"), fileId = url.searchParams.get("fileId");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  if (!uid) throw new Error("Chưa đăng nhập.");
  if (!fileId) throw new Error("Thiếu fileId.");
  const dat = await resolveDriveDat(env, uid, account0, fileId);
  if (!dat) throw new Error("Không tìm thấy file trong kho nào (token kho có thể hỏng — bấm 🩺 Kiểm token + quyền).");
  await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}/permissions?supportsAllDrives=true`,
    { method: "POST", headers: { Authorization: `Bearer ${dat}`, "content-type": "application/json" }, body: JSON.stringify({ role: "reader", type: "anyone" }) });
  const m = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}?fields=webViewLink&supportsAllDrives=true`, { headers: { Authorization: `Bearer ${dat}` } });
  const mj = await m.json().catch(() => ({}));
  return json({ ok: true, link: mj.webViewLink || `https://drive.google.com/file/d/${fileId}/view` });
}

// GET /api/drive-thumb?t=&fileId=[&account=] -> ẢNH THUMBNAIL video (Drive tự tạo) để xem lướt như Google Drive.
async function apiDriveThumb(request, url, env) {
  const t = url.searchParams.get("t"), account0 = url.searchParams.get("account"), fileId = url.searchParams.get("fileId");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  if (!uid || !fileId) return new Response("", { status: 400 });
  const dat = await resolveDriveDat(env, uid, account0, fileId);
  if (!dat) return new Response("", { status: 404 });
  const m = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}?fields=thumbnailLink,hasThumbnail&supportsAllDrives=true`, { headers: { Authorization: `Bearer ${dat}` } });
  const mj = await m.json().catch(() => ({}));
  if (!mj.thumbnailLink) return new Response("", { status: 404 });   // Drive chưa tạo thumb (video mới upload) -> để client dùng placeholder
  const big = mj.thumbnailLink.replace(/=s\d+$/, "=s640");
  const im = await fetch(big, { headers: { Authorization: `Bearer ${dat}` } });
  const h = new Headers();
  h.set("content-type", im.headers.get("content-type") || "image/jpeg");
  h.set("Access-Control-Allow-Origin", "*");
  h.set("Cache-Control", "private, max-age=1800");
  return new Response(im.body, { status: im.status, headers: h });
}

// POST /api/upload-init {t,account,type(long|short),name,mimeType,size}
//   -> Tạo PHIÊN UPLOAD resumable trực tiếp trên Drive của kho, trả sessionUri để BROWSER tự PUT bytes
//      (KHÔNG qua Worker -> full tốc độ, KHÔNG giảm chất lượng, token KHÔNG lộ ra browser).
async function apiUploadInit(request, url, env) {
  const b = request.method === "POST" ? await request.json().catch(() => ({})) : {};
  const { t, account, type, name, mimeType, size, folderId, overwrite } = b;
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  if (!uid) throw new Error("Chưa đăng nhập.");
  if (!account || !name) throw new Error("Thiếu account/tên file.");
  const { conn, dat } = await driveCtx(env, uid, account);
  let folder;
  if (folderId) {
    folder = folderId;   // TẢI THẲNG vào 1 thư mục đang duyệt (như kéo trên Drive)
    if (overwrite) {     // thay thế: đưa file trùng tên trong folder này vào thùng rác trước
      try {
        const q = encodeURIComponent(`'${folder}' in parents and name='${String(name).replace(/'/g, "\\'")}' and trashed=false`);
        const ex = await (await fetch(`https://www.googleapis.com/drive/v3/files?q=${q}&fields=files(id)&pageSize=5`,
          { headers: { Authorization: `Bearer ${dat}` } })).json();
        for (const f of (ex.files || [])) await fetch(`https://www.googleapis.com/drive/v3/files/${f.id}`,
          { method: "PATCH", headers: { Authorization: `Bearer ${dat}`, "content-type": "application/json" }, body: JSON.stringify({ trashed: true }) });
      } catch (e) {}
    }
  } else {
    const root = conn.root;
    if (!root) throw new Error("Kho chưa có thư mục gốc (kết nối lại Drive).");
    const queue = await driveChildFolder(dat, root, "_QUEUE");
    folder = await driveChildFolder(dat, queue, type === "short" ? "short" : "long");
  }
  const init = await fetch("https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,name",
    { method: "POST",
      headers: { Authorization: `Bearer ${dat}`, "content-type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": mimeType || "video/mp4",
        ...(size ? { "X-Upload-Content-Length": String(size) } : {}) },
      body: JSON.stringify({ name, parents: [folder] }) });
  if (!init.ok) throw new Error("Init upload lỗi: " + init.status + " " + (await init.text()).slice(0, 200));
  const sessionUri = init.headers.get("location") || init.headers.get("Location");
  if (!sessionUri) throw new Error("Không lấy được session upload.");
  return json({ ok: true, sessionUri, folderId: folder });
}

// POST /api/upload-chunk?s=<sessionUri>  header X-Mm0-Range: <Content-Range>  body=raw bytes 1 chunk
//   -> RELAY chunk lên Drive session (browser không PUT thẳng được vì CORS). Trả 308 (còn tiếp) / 200-201 (xong).
async function apiUploadChunk(request, url, env) {
  const sess = url.searchParams.get("s");
  const range = request.headers.get("x-mm0-range") || "";
  if (!sess) throw new Error("Thiếu session.");
  let host = "";
  try { host = new URL(sess).hostname; } catch (e) { throw new Error("Session không hợp lệ."); }
  if (!/(^|\.)googleapis\.com$/.test(host)) throw new Error("Session không phải Google.");
  const buf = await request.arrayBuffer();   // buffer 1 chunk (<=16MB) -> có Content-Length chuẩn
  const put = await fetch(sess, { method: "PUT", headers: range ? { "Content-Range": range } : {}, body: buf });
  const status = put.status;
  let id = "";
  if (status === 200 || status === 201) { try { id = (await put.json()).id || ""; } catch (_) {} }
  return json({ ok: status === 200 || status === 201 || status === 308, status, id });
}

// POST /api/upload-done {t,account,folderId,base,sidecar} -> tạo sidecar .json cạnh video (định tuyến kênh).
async function apiUploadDone(request, url, env) {
  const b = request.method === "POST" ? await request.json().catch(() => ({})) : {};
  const { t, account, folderId, base, sidecar } = b;
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  if (!uid) throw new Error("Chưa đăng nhập.");
  if (!account || !folderId || !base) throw new Error("Thiếu tham số.");
  const { dat } = await driveCtx(env, uid, account);
  const boundary = "mm0" + Math.abs((Date.now() ^ (base.length * 2654435761)) >>> 0).toString(36);
  const meta = { name: `${base}.json`, parents: [folderId] };
  const body = `--${boundary}\r\ncontent-type: application/json; charset=UTF-8\r\n\r\n${JSON.stringify(meta)}\r\n` +
    `--${boundary}\r\ncontent-type: application/json\r\n\r\n${JSON.stringify(sidecar || {})}\r\n--${boundary}--`;
  const r = await fetch("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id",
    { method: "POST", headers: { Authorization: `Bearer ${dat}`, "content-type": `multipart/related; boundary=${boundary}` }, body });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error("Ghi sidecar lỗi: " + ((j.error && j.error.message) || r.status));
  return json({ ok: true, id: j.id });
}

// POST /api/file-action {t,account,action,fileId,newName}
//   action: rename | trash | untrash   (xoá = đưa vào THÙNG RÁC Drive, khôi phục được)
async function apiFileAction(request, url, env) {
  const body = request.method === "POST" ? await request.json().catch(() => ({})) : {};
  const { t, account, action, fileId, newName } = body;
  if (!t) throw new Error("Thiếu token đăng nhập.");
  if (!account || !fileId || !action) throw new Error("Thiếu tham số.");
  const uid = await verifyIdToken(t, env.FIREBASE_PROJECT_ID);
  let patch;
  if (action === "rename") { if (!newName) throw new Error("Thiếu tên mới."); patch = { name: newName }; }
  else if (action === "trash") patch = { trashed: true };
  else if (action === "untrash") patch = { trashed: false };
  else throw new Error("action không hỗ trợ: " + action);
  const doPatch = async (dat) => {
    const r = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(fileId)}?fields=id,name,trashed&supportsAllDrives=true`,
      { method: "PATCH", headers: { Authorization: `Bearer ${dat}`, "content-type": "application/json" }, body: JSON.stringify(patch) });
    const j = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, j };
  };
  // 1) thử token kho đã ghi
  let dat = null; try { dat = (await driveCtx(env, uid, account)).dat; } catch (_) { }
  let res = dat ? await doPatch(dat) : { ok: false, status: 0, j: {} };
  // 2) LỖI QUYỀN (403/404): có thể bản ghi kho LỆCH -> dò đúng kho SỞ HỮU file rồi thử lại (tự chữa).
  if (!res.ok && (res.status === 403 || res.status === 404 || res.status === 0)) {
    const owner = await findFileOwnerDat(env, uid, fileId);
    if (owner) { const r2 = await doPatch(owner.dat); if (r2.ok) res = r2; }
  }
  if (!res.ok) {
    const msg = (res.j.error && res.j.error.message) || ("Drive " + res.status);
    if (/insufficient|permission|forbidden|403/i.test(msg))
      throw new Error(`Kho "${account}" chưa đủ quyền xóa/sửa file này — vào Storage, KẾT NỐI LẠI tài khoản Drive này và TICK ĐỦ QUYỀN ở màn hình Google. (${msg})`);
    throw new Error(msg);
  }
  return json({ ok: true, ...res.j });
}

/* ---------- YouTube access token từ refresh_token (retry lỗi TẠM, lộ lỗi THẬT) ---------- */
async function ytAccessToken(client_id, client_secret, refresh_token) {
  let last = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    let res, r;
    try {
      res = await fetch("https://oauth2.googleapis.com/token", {
        method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ client_id, client_secret, refresh_token, grant_type: "refresh_token" }),
      });
      r = await res.json();
    } catch (e) { last = "network:" + (e && e.message || e); await new Promise((s) => setTimeout(s, 800 * (attempt + 1))); continue; }
    if (r && r.access_token) return r.access_token;
    // invalid_grant / invalid_client = HỎNG THẬT (reconnect); còn lại có thể tạm -> thử lại
    const err = (r && r.error) || ("http_" + res.status);
    last = err + (r && r.error_description ? (": " + r.error_description) : "");
    if (err === "invalid_grant" || err === "invalid_client" || err === "unauthorized_client") {
      const e = new Error("TOKEN_INVALID: " + last); e.tokenInvalid = true; throw e;
    }
    await new Promise((s) => setTimeout(s, 800 * (attempt + 1)));   // lỗi tạm -> backoff rồi thử lại
  }
  throw new Error("Không lấy được access token (thử 3 lần): " + last);
}

const YT_SCOPES = [
  "https://www.googleapis.com/auth/youtube.upload",
  "https://www.googleapis.com/auth/youtube",
  "https://www.googleapis.com/auth/youtube.force-ssl",
  "https://www.googleapis.com/auth/yt-analytics.readonly",   // phân tích toàn kênh theo kỳ
  "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",   // 23/8: đọc DOANH THU (xin sẵn 1 lần — kênh connect sau này khỏi re-consent khi bật báo cáo tiền)
  "https://www.googleapis.com/auth/userinfo.email",
].join(" ");
const DRIVE_SCOPES = [
  // 23/8: ĐỔI auth/drive (FULL = scope RESTRICTED -> app chưa verify bị Google phát token 7 NGÀY
  // + mỗi account cấp quyền ăn 1 suất user-cap 100) sang drive.file (NON-SENSITIVE: token vĩnh
  // viễn, không cần verify, không ăn cap). Đủ dùng vì hệ CHỈ đụng file do chính app tạo
  // (_QUEUE/video/thumb/sidecar) — kể cả file cũ upload bằng token full trước đây vẫn thấy
  // (drive.file tính "file của app" theo OAuth client, không theo scope lúc tạo).
  // 23/8 ROLLBACK: quay lại 'drive' FULL. Lý do: 70 kho đang chạy có refresh_token cấp theo scope
  // 'drive'; đổi scope trong code làm MỌI lần refresh trả invalid_scope -> toàn bộ kho chết ngay
  // lập tức (đo thật: "Không tài khoản kho nào đủ chỗ" trên mọi lane). Muốn dùng drive.file thì
  // phải đổi TỪNG KHO ĐÚNG LÚC RECONNECT, không được đổi đồng loạt trong code.
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
  // 3) danh sách Page + page token + AVATAR (lấy luôn trong 1 call, không tốn thêm request)
  const pages = await (await fetch(`https://graph.facebook.com/v19.0/me/accounts?fields=id,name,access_token,picture.width(100).height(100){url}&access_token=${userTok}`)).json();
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
    const page_avatar = (((pg.picture || {}).data) || {}).url || "";
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
      { channel: slug, kind: "facebook", owner: uid, page_id: pg.id, page_name: pg.name, page_avatar,
        page_token: pg.access_token, ig_user_id, ig_username, ig_name, ig_avatar,
        fb_owner_id, fb_owner_name, connected_at: new Date().toISOString() });
    await fsPatch(env, at, `fb_pages/${uid}__${slug}`,
      { name: slug, owner: uid, page_id: pg.id, page_name: pg.name, page_avatar,
        ig_user_id, ig_username, ig_name, ig_avatar, fb_owner_id, fb_owner_name, fb_ok: true, connected_at: new Date().toISOString() },
      ["name", "owner", "page_id", "page_name", "page_avatar", "ig_user_id", "ig_username", "ig_name", "ig_avatar", "fb_owner_id", "fb_owner_name", "fb_ok", "connected_at"]);
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
let _saCache = {};   // CACHE token SA theo scope (TTL ~55') -> đỡ tốn subrequest khi dò nhiều kho (giới hạn 50/lần Cloudflare)
async function saAccessToken(env, scope = "https://www.googleapis.com/auth/datastore") {
  const nowMs = Date.now();
  if (_saCache[scope] && _saCache[scope].exp > nowMs) return _saCache[scope].tok;
  const now = Math.floor(nowMs / 1000);
  const claim = {
    iss: env.SA_CLIENT_EMAIL,
    scope,
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
  _saCache[scope] = { tok: r.access_token, exp: nowMs + 55 * 60 * 1000 };   // cache 55'
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
  let res = await fetch(u, { headers: { Authorization: `Bearer ${accessToken}` } });
  if (res.status === 429) {                 // Firestore rate-limit (kiểm nhiều kho dồn) -> chờ ngắn rồi thử lại 1 lần
    await new Promise(r => setTimeout(r, 500));
    res = await fetch(u, { headers: { Authorization: `Bearer ${accessToken}` } });
  }
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

// ---- LINK RÚT GỌN THƯƠNG HIỆU (redirect + đếm click) ----
// /go/<code> -> đọc links/<code> -> 302 tới url đích (nối UTM/query đang có) -> đếm click nền (không chặn).
async function handleGo(url, env, ctx) {
  const code = decodeURIComponent(url.pathname.slice(4)).trim().replace(/\/+$/, "");
  const notFound = () => new Response(
    "<!doctype html><meta charset=utf-8><title>Link</title><body style='font-family:system-ui;background:#0b0f1a;color:#e5e7eb;display:grid;place-items:center;height:100vh;margin:0'><div style='text-align:center'><h2>🔗 Link không tồn tại</h2><p style='color:#9ca3af'>Mã liên kết không đúng hoặc đã bị gỡ.</p></div>",
    { status: 404, headers: { "content-type": "text/html; charset=utf-8" } });
  if (!code) return notFound();
  let doc = null;
  try {
    const at = await saAccessToken(env);
    doc = await fsGet(env, at, `links/${encodeURIComponent(code)}`);
    if (doc && doc.url) {
      // đếm click nền (không để chậm redirect)
      const inc = fsPatch(env, at, `links/${encodeURIComponent(code)}`,
        { clicks: Number(doc.clicks || 0) + 1, last_click: new Date().toISOString() },
        ["clicks", "last_click"]).catch(() => {});
      if (ctx && ctx.waitUntil) ctx.waitUntil(inc);
    }
  } catch (e) { /* lỗi Firestore -> coi như không tìm thấy */ }
  if (!doc || !doc.url) return notFound();
  // Nối query đang có (VD utm bổ sung) vào link đích
  let target = String(doc.url);
  const extra = url.search ? url.search.slice(1) : "";
  if (extra) target += (target.includes("?") ? "&" : "?") + extra;
  return new Response(null, { status: 302, headers: { Location: target, "cache-control": "no-store" } });
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
  h.set("Access-Control-Allow-Headers", "content-type,x-mm0-range");
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

// ── CLOUDFLARE WORKERS AI: tra Account ID từ token (22/8) ────────────────────────────────────
// Trình duyệt KHÔNG gọi thẳng api.cloudflare.com được (CORS chặn) -> dashboard gửi token vào đây,
// Worker (server-side, miễn CORS) hỏi /accounts rồi trả về account đầu tiên. Token chỉ đi qua,
// KHÔNG lưu ở đâu cả.
async function apiCfAccounts(request) {
  if (request.method !== "POST") return new Response(JSON.stringify({ error: "POST only" }), { status: 405 });
  let tok = "";
  try { tok = ((await request.json()) || {}).token || ""; } catch (e) {}
  if (!tok || tok.length < 20) return new Response(JSON.stringify({ error: "thiếu token" }), { status: 400 });
  const r = await fetch("https://api.cloudflare.com/client/v4/accounts", {
    headers: { Authorization: "Bearer " + tok } });
  const j = await r.json().catch(() => ({}));
  const acc = ((j || {}).result || [])[0] || {};
  // /accounts trả RỖNG dù token sống (CF lọc im lặng khi token thiếu quyền đọc account — bẫy!)
  // -> THỬ account nhà (nơi worker này đang chạy): token dùng được ở đó thì lấy luôn id đó.
  if (!acc.id) {
    const home = "bef1f9158d2eb75d29527778f5c59bf1";   // Account ID của account Cloudflare chính (adisondurham)
    try {
      const p = await fetch(`https://api.cloudflare.com/client/v4/accounts/${home}/ai/models/search?per_page=1`,
        { headers: { Authorization: "Bearer " + tok } });
      if (p.ok) { acc.id = home; acc.name = "home"; }
    } catch (e) {}
  }
  // Vẫn không ra -> verify token để phân biệt "token sai" vs "token sống nhưng thiếu quyền"
  let verify = "";
  if (!acc.id) {
    try {
      const v2 = await fetch("https://api.cloudflare.com/client/v4/user/tokens/verify", {
        headers: { Authorization: "Bearer " + tok } });
      const jv = await v2.json().catch(() => ({}));
      verify = v2.ok ? (((jv || {}).result || {}).status || "active") : "invalid";
    } catch (e) { verify = "network"; }
  }
  return new Response(JSON.stringify({ ok: r.ok, status: r.status, id: acc.id || "", name: acc.name || "", verify }),
    { headers: { "content-type": "application/json" } });
}


// ── VẼ THỬ FLUX qua worker (22/8): dashboard/thử-nghiệm gọi với {token, account, prompt, steps}
// -> trả {image: base64}. Token chỉ đi qua, không lưu.
async function apiCfFlux(request) {
  if (request.method !== "POST") return new Response(JSON.stringify({ error: "POST only" }), { status: 405 });
  let b = {};
  try { b = await request.json(); } catch (e) {}
  if (!b.token || !b.account || !b.prompt) return new Response(JSON.stringify({ error: "thiếu token/account/prompt" }), { status: 400 });
  const r = await fetch(`https://api.cloudflare.com/client/v4/accounts/${b.account}/ai/run/@cf/black-forest-labs/flux-1-schnell`, {
    method: "POST",
    headers: { Authorization: "Bearer " + b.token, "content-type": "application/json" },
    body: JSON.stringify({ prompt: String(b.prompt).slice(0, 1800), steps: Math.min(Number(b.steps) || 4, 8),
      ...(b.seed !== undefined ? { seed: Number(b.seed) } : {}) }) });   // width/height: flux schnell KHÔNG nhận (5006) — đã gỡ
  const j = await r.json().catch(() => ({}));
  return new Response(JSON.stringify({ ok: r.ok, status: r.status,
    image: (((j || {}).result) || {}).image || "", err: JSON.stringify((j || {}).errors || "").slice(0, 200) }),
    { headers: { "content-type": "application/json" } });
}

// POST /api/r2-setup {token, bucket?} -> tạo bucket + khoá S3 cho R2, trả chuỗi "r2:acc:akid:secret:bucket"
// 23/8 (user: "thêm phần get key R2 tự động như driver"): tự làm hết 3 việc tay — tạo bucket, tạo
// API token cấp account với quyền R2, rồi đổi token đó thành cặp khoá S3.
// Quy tắc của Cloudflare: Access Key ID = ID của token, Secret = SHA-256 của giá trị token.
async function apiR2Setup(request) {
  if (request.method !== "POST") return new Response(JSON.stringify({ error: "POST only" }), { status: 405 });
  let body = {};
  try { body = (await request.json()) || {}; } catch (e) {}
  const tok = String(body.token || "").trim();
  const bucket = String(body.bucket || "mm0-park").trim();
  if (tok.length < 20) return json({ error: "Thiếu token Cloudflare." });
  const H = { Authorization: "Bearer " + tok, "content-type": "application/json" };
  const api = "https://api.cloudflare.com/client/v4";

  // 1) account id — token cấp account chỉ thấy đúng account của nó
  let acc = "";
  try {
    const r = await fetch(`${api}/accounts`, { headers: H });
    const j = await r.json().catch(() => ({}));
    acc = (((j || {}).result || [])[0] || {}).id || "";
  } catch (e) {}
  if (!acc) return json({ error: "Token không đọc được Account — khi tạo token nhớ tick 'Account Settings: Read'." });

  // 2) bucket (đã có thì bỏ qua)
  try {
    const rb = await fetch(`${api}/accounts/${acc}/r2/buckets`, {
      method: "POST", headers: H, body: JSON.stringify({ name: bucket }) });
    const jb = await rb.json().catch(() => ({}));
    const err = ((jb || {}).errors || [])[0] || {};
    const dup = /exists|already|duplicate/i.test(String(err.message || ""));
    if (!rb.ok && !dup) {
      const msg = String(err.message || rb.status);
      // 23/8: phân biệt 2 ca hoàn toàn khác nhau — trước gộp làm một nên báo oan cho token.
      if (/enable r2|not enabled|subscription/i.test(msg)) {
        return json({ error: "Tài khoản Cloudflare này CHƯA BẬT R2 (token của anh vẫn tốt, không phải tạo lại).\n\n" +
                             "Cách bật 1 lần: mở dash.cloudflare.com → menu R2 Object Storage bên trái → Enable/Purchase R2.\n" +
                             "Cloudflare đòi gắn thẻ ở bước này nhưng KHÔNG trừ tiền khi dùng dưới 10GB.\n\n" +
                             "Bật xong quay lại dán đúng token cũ và bấm lại nút này." });
      }
      if (/permission|denied|unauthorized|9109|authentication/i.test(msg)) {
        return json({ error: `Token thiếu quyền: ${msg}. Tạo lại token và tick 'Workers R2 Storage: Edit'.` });
      }
      return json({ error: `Không tạo được bucket: ${msg}` });
    }
  } catch (e) { return json({ error: "Lỗi mạng khi tạo bucket: " + String(e).slice(0, 60) }); }

  // 3) nhóm quyền R2 (id khác nhau theo account nên phải tra, không hardcode được)
  let pg = "";
  try {
    const rp = await fetch(`${api}/accounts/${acc}/tokens/permission_groups`, { headers: H });
    const jp = await rp.json().catch(() => ({}));
    const list = (jp || {}).result || [];
    const hit = list.find(g => /r2/i.test(g.name || "") && /write|edit/i.test(g.name || ""))
             || list.find(g => /r2/i.test(g.name || ""));
    pg = (hit || {}).id || "";
  } catch (e) {}
  if (!pg) return json({ error: "Không tra được nhóm quyền R2 — token cần quyền 'API Tokens: Edit' để tự tạo khoá." });

  // 4) tạo token cấp account -> đổi thành cặp khoá S3
  try {
    const rt = await fetch(`${api}/accounts/${acc}/tokens`, {
      method: "POST", headers: H,
      body: JSON.stringify({
        name: `mm0-r2-${bucket}-${Math.random().toString(36).slice(2, 7)}`,
        policies: [{ effect: "allow",
                     permission_groups: [{ id: pg }],
                     resources: { [`com.cloudflare.api.account.${acc}`]: "*" } }] }) });
    const jt = await rt.json().catch(() => ({}));
    if (!rt.ok || !((jt || {}).result || {}).value) {
      const e0 = ((jt || {}).errors || [])[0] || {};
      return json({ error: `Không tạo được khoá R2: ${e0.message || rt.status}. Token cần quyền 'API Tokens: Edit'.` });
    }
    const akid = jt.result.id;
    const raw = jt.result.value;
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(raw));
    const secret = [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
    return json({ ok: true, key: `r2:${acc}:${akid}:${secret}:${bucket}`, bucket, account: acc });
  } catch (e) {
    return json({ error: "Lỗi tạo khoá R2: " + String(e).slice(0, 70) });
  }
}
