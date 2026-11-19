/**
 * CongCuShop — checkAccess.js (file dùng chung cho cả 6 trang tĩnh)
 * ================================================================
 * NHIỆM VỤ: hỏi Supabase "user này có được dùng sản phẩm này không",
 * rồi khóa/mở tính năng trên trang + hiện nút "Gia hạn bằng VietQR".
 *
 * NGUYÊN TẮC:
 *  - File này CHỈ hiển thị kết quả, KHÔNG tự quyết định quyền.
 *    Quyền thật do hàm SQL get_access() quyết (trial còn hạn / đã trả tiền).
 *  - Trial 3 ngày được đếm Ở SERVER (trial_ends_at trong DB), không đếm
 *    bằng localStorage — xóa storage cũng không reset được trial.
 *  - Device fingerprint: CHỈ hash SHA-256 được gửi lên server. userAgent,
 *    độ phân giải, canvas... KHÔNG BAO GIỜ rời khỏi máy user.
 *
 * CÁCH DÙNG (chèn sau khi trang load, SAU khi agent chỉnh giao diện xong):
 *   <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js"></script>
 *   <script src="/shared/checkAccess.js"></script>
 *   <script>
 *     CongCuShop.init({ supabaseUrl: 'https://<ref>.supabase.co', anonKey: '<anon key>', product: 'p1' });
 *     CongCuShop.checkAccess('p1'); // tự khóa/mở theo [data-requires-access] và [data-paywall]
 *   </script>
 *
 * Quy ước HTML trên trang sản phẩm:
 *   [data-requires-access]  — phần tính năng chính, ẩn khi hết quyền.
 *   [data-paywall]          — khung "Hết dùng thử / Gia hạn", hiện khi hết quyền.
 *   [data-trial-days-left]  — span hiện "còn X ngày dùng thử".
 *   [data-pay-button]       — nút "Gia hạn bằng VietQR" (tự gắn sự kiện).
 */

(function (global) {
  "use strict";

  var SUPABASE_CDN = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/dist/umd/supabase.min.js";

  var cfg = { supabaseUrl: "", anonKey: "", product: "" };
  var client = null; // supabase client
  var accessCache = null;

  function loadSupabaseUmd() {
    return new Promise(function (resolve, reject) {
      if (global.supabase && global.supabase.createClient) return resolve();
      var s = document.createElement("script");
      s.src = SUPABASE_CDN;
      s.onload = resolve;
      s.onerror = function () { reject(new Error("load_supabase_failed")); };
      document.head.appendChild(s);
    });
  }

  function init(options) {
    cfg.supabaseUrl = options.supabaseUrl;
    cfg.anonKey = options.anonKey;
    cfg.product = options.product || cfg.product;
    return loadSupabaseUmd().then(function () {
      client = global.supabase.createClient(cfg.supabaseUrl, cfg.anonKey);
      return client;
    });
  }

  function edgeUrl(fn) {
    return cfg.supabaseUrl.replace(/\/$/, "") + "/functions/v1/" + fn;
  }

  function authHeaders(session) {
    return {
      "Content-Type": "application/json",
      apikey: cfg.anonKey,
      Authorization: "Bearer " + session.access_token,
    };
  }

  // --- Device fingerprint: chỉ hash rời khỏi máy -------------------------
  // Thành phần: userAgent, màn hình, timezone, ngôn ngữ, canvas, WebGL.
  // Tất cả giá trị thô chỉ tồn tại trong bộ nhớ rồi hash; server chỉ nhận
  // chuỗi hex 64 ký tự. Đây là lớp phát hiện lạm dụng trial, không phải
  // theo dõi người dùng — xem README.md mục chống lạm dụng.
  function canvasHash() {
    try {
      var c = document.createElement("canvas");
      c.width = 200; c.height = 50;
      var ctx = c.getContext("2d");
      ctx.textBaseline = "top";
      ctx.font = "14px Arial";
      ctx.fillStyle = "#f60"; ctx.fillRect(0, 0, 100, 25);
      ctx.fillStyle = "#069"; ctx.fillText("congcushop", 2, 15);
      ctx.fillStyle = "rgba(102, 204, 0, 0.7)"; ctx.fillText("congcushop", 4, 17);
      return c.toDataURL();
    } catch (e) { return "no-canvas"; }
  }

  function webglHash() {
    try {
      var c = document.createElement("canvas");
      var gl = c.getContext("webgl") || c.getContext("experimental-webgl");
      if (!gl) return "no-webgl";
      var ext = gl.getExtension("WEBGL_debug_renderer_info");
      var renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : "unknown";
      return String(renderer);
    } catch (e) { return "no-webgl"; }
  }

  function sha256Hex(str) {
    var buf = new TextEncoder().encode(str);
    return crypto.subtle.digest("SHA-256", buf).then(function (hash) {
      return Array.from(new Uint8Array(hash))
        .map(function (b) { return b.toString(16).padStart(2, "0"); })
        .join("");
    });
  }

  function getDeviceFingerprint() {
    var parts = [
      navigator.userAgent || "",
      (screen.width || 0) + "x" + (screen.height || 0) + "x" + (screen.colorDepth || 0),
      (Intl.DateTimeFormat().resolvedOptions().timeZone) || "",
      navigator.language || "",
      canvasHash(),
      webglHash(),
    ];
    return sha256Hex(parts.join("|"));
  }

  // --- API ---------------------------------------------------------------
  function getSession() {
    return client.auth.getSession().then(function (r) { return r.data.session; });
  }

  // Hỏi quyền từ server (hàm SQL get_access). Kết quả cache trong phiên.
  function checkAccess(product) {
    product = product || cfg.product;
    return getSession().then(function (session) {
      if (!session) return { access: false, reason: "login_required" };
      return client.rpc("get_access", { p_product: product }).then(function (res) {
        if (res.error) return { access: false, reason: "rpc_error", detail: res.error.message };
        accessCache = res.data;
        return res.data;
      });
    });
  }

  // Bắt đầu trial 3 ngày (server tạo, kèm fingerprint để chống lạm dụng).
  function startTrial(product) {
    product = product || cfg.product;
    return Promise.all([getSession(), getDeviceFingerprint()]).then(function (arr) {
      var session = arr[0], fp = arr[1];
      if (!session) return { access: false, reason: "login_required" };
      return fetch(edgeUrl("start-trial"), {
        method: "POST",
        headers: authHeaders(session),
        body: JSON.stringify({ product: product, device_fingerprint: fp }),
      }).then(function (r) { return r.json(); });
    });
  }

  // Tạo link payOS -> redirect sang trang thanh toán VietQR.
  function createPayment(product) {
    product = product || cfg.product;
    return getSession().then(function (session) {
      // Chưa đăng nhập: trả kèm thông báo rõ để nơi gọi (snippet, nút Pro
      // P9...) hiển thị thay vì "lỗi không rõ". Không redirect ở đây.
      if (!session) return { access: false, reason: "login_required", error: "Vui lòng đăng nhập để dùng thử 3 ngày miễn phí." };
      return fetch(edgeUrl("create-payment"), {
        method: "POST",
        headers: authHeaders(session),
        body: JSON.stringify({ product: product }),
      }).then(function (r) { return r.json(); }).then(function (res) {
        if (res.checkoutUrl) window.location.href = res.checkoutUrl;
        return res;
      });
    });
  }

  // --- UI: khóa/mở theo kết quả checkAccess ------------------------------
  // Hiện câu yêu cầu đăng nhập + cuộn tới khối đăng nhập (#ccs-auth).
  // Dùng khi bấm nút thanh toán lúc chưa đăng nhập (trước đây im lặng).
  function showLoginRequired() {
    document.querySelectorAll("[data-paywall-reason]").forEach(function (el) {
      el.textContent = paywallMessage("login_required");
    });
    var auth = document.getElementById("ccs-auth");
    if (auth && auth.scrollIntoView) {
      auth.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function applyAccessToPage(access) {
    var locked = !access.access;
    if (document.body) document.body.classList.toggle("ccs-locked", locked);
    document.querySelectorAll("[data-requires-access]").forEach(function (el) {
      el.style.display = locked ? "none" : "";
    });
    document.querySelectorAll("[data-paywall]").forEach(function (el) {
      if (locked) { el.removeAttribute("hidden"); el.style.display = "block"; }
      else { el.style.display = "none"; }
    });
    document.querySelectorAll("[data-trial-days-left]").forEach(function (el) {
      el.textContent = ((access.reason === "trial" || access.reason === "bundle_trial") && access.days_left != null)
        ? ("Còn " + access.days_left + " ngày dùng thử")
        : "";
    });
    document.querySelectorAll("[data-paywall-reason]").forEach(function (el) {
      el.textContent = paywallMessage(access.reason);
    });
    document.querySelectorAll("[data-pay-button]").forEach(function (btn) {
      if (!btn.dataset.bound) {
        btn.dataset.bound = "1";
        btn.addEventListener("click", function () {
          // SỬA LỖI mục 13(b): trước đây bấm nút lúc chưa đăng nhập thì
          // createPayment trả login_required trong im lặng (không chuyển
          // trang, không báo lỗi). Giờ hiện câu yêu cầu đăng nhập và cuộn
          // tới khối đăng nhập; lỗi server cũng hiện rõ thay vì im lặng.
          // Nút có data-product riêng thì dùng nó, không thì dùng sản phẩm
          // của trang (cfg.product, do snippet nạp từ data-product).
          var product = btn.getAttribute("data-product") || cfg.product;
          createPayment(product).then(function (res) {
            if (!res) return;
            if (res.reason === "login_required") {
              showLoginRequired();
            } else if (!res.checkoutUrl && res.error) {
              document.querySelectorAll("[data-paywall-reason]").forEach(function (el) {
                el.textContent = res.error;
              });
            }
            // Có checkoutUrl thì createPayment đã tự redirect sang payOS.
          }).catch(function () {
            document.querySelectorAll("[data-paywall-reason]").forEach(function (el) {
              el.textContent = "Lỗi kết nối — thử lại sau.";
            });
          });
        });
      }
    });
  }

  function paywallMessage(reason) {
    switch (reason) {
      case "trial_expired": return "Đã hết 3 ngày dùng thử. Gia hạn để dùng tiếp.";
      case "subscription_expired": return "Gói tháng đã hết hạn. Gia hạn để dùng tiếp.";
      case "verification_required": return "Tài khoản cần xác minh thêm trước khi dùng thử. Vui lòng liên hệ hỗ trợ để được duyệt.";
      case "login_required": return "Vui lòng đăng nhập để dùng thử 3 ngày miễn phí.";
      case "no_trial": return "Bắt đầu dùng thử 3 ngày miễn phí.";
      default: return "Gia hạn để dùng tiếp.";
    }
  }

  global.CongCuShop = {
    init: init,
    getSession: getSession,
    checkAccess: checkAccess,
    startTrial: startTrial,
    createPayment: createPayment,
    getDeviceFingerprint: getDeviceFingerprint,
    applyAccessToPage: applyAccessToPage,
    paywallMessage: paywallMessage,
  };
})(window);
