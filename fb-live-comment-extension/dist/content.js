console.log("FB Live Auto Comment bridge loaded");

const PROFILE_API = "http://127.0.0.1:5003/account_profile";
const POLL_INTERVAL = 4000;
const COMMENT_DELAY = 800;
const SESSION_CONFIG_KEY = "fb_auto_session";

let isPolling = false;
let lastReportedProfile = "";
const sentReplies = new Set();
const autoConfig = readAutoConfig();
const API_BASE = `http://127.0.0.1:${autoConfig.llmPort}`;

function readAutoConfig() {
  const searchParams = new URLSearchParams(window.location.search);
  const hashParams = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const account = searchParams.get("fb_auto_account") || hashParams.get("fb_auto_account") || "";
  const jsonlFile = searchParams.get("fb_auto_jsonl") || hashParams.get("fb_auto_jsonl") || "";
  const llmPortText = searchParams.get("fb_auto_port") || hashParams.get("fb_auto_port") || "";

  let saved = {};
  try {
    saved = JSON.parse(sessionStorage.getItem(SESSION_CONFIG_KEY) || "{}");
  } catch (error) {
    sessionStorage.removeItem(SESSION_CONFIG_KEY);
  }
  if (account && jsonlFile) {
    saved = { account, jsonlFile, llmPort: llmPortText };
    sessionStorage.setItem(SESSION_CONFIG_KEY, JSON.stringify(saved));
  } else if (account) {
    sessionStorage.removeItem(SESSION_CONFIG_KEY);
    saved = {};
  }

  const activeAccount = account || saved.account || "";
  const activeJsonlFile = jsonlFile || saved.jsonlFile || "";
  const streamId = activeJsonlFile.split(/[\\/]/).pop().replace(/\.jsonl$/i, "");
  const accountNumber = Number((activeAccount.match(/_(\d+)$/) || [])[1]);
  const rowNumber = Number((streamId.match(/_live_(\d+)$/) || [])[1]);
  const configuredPort = Number(llmPortText || saved.llmPort);
  const fallbackPort = 5002 + Math.max(accountNumber - 1, 0) * 5 + Math.max(rowNumber - 1, 0);
  const llmPort = configuredPort >= 5002 && configuredPort <= 6000
    ? configuredPort : fallbackPort;

  return {
    account: activeAccount,
    jsonlFile: activeJsonlFile,
    streamId,
    llmPort
  };
}

function cleanProfileName(value) {
  const original = String(value || "").replace(/^\(\d+\+?\)\s*/, "").trim();
  if (/^(?:你的?|我|your)\s*(?:的)?\s*(?:個人檔案|大頭貼|頭貼|profile picture|profile photo)?$/i.test(original)) return "";
  const name = original
    .replace(/\s*[|·–-]\s*Facebook.*$/i, "")
    .replace(/^profile (?:picture|photo) of\s+/i, "")
    .replace(/(?:的|'s)?\s*(?:個人檔案|大頭貼|頭貼|profile picture|profile photo)$/i, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!name || /^(facebook(?: live)?|登入|log in|profile|your|your profile|你|我|你的|你的個人檔案)$/i.test(name)) return "";
  return name.slice(0, 80);
}

function navbarProfileHint() {
  for (const image of document.querySelectorAll('header img, nav img, [role="banner"] img')) {
    const alt = image.getAttribute("alt") || "";
    if (!/大頭貼|頭貼|profile picture|profile photo/i.test(alt)) continue;
    const profileLink = image.closest('a, [role="button"]');
    return {
      displayName: cleanProfileName(alt) || cleanProfileName(profileLink?.getAttribute("aria-label"))
    };
  }
  return { displayName: "" };
}

function ownProfileMetadata(profilePage) {
  const title = profilePage.querySelector('meta[property="og:title"]')?.content;
  const heading = profilePage.querySelector('h1')?.textContent;
  return {
    displayName: cleanProfileName(title) || cleanProfileName(heading) || cleanProfileName(profilePage.title)
  };
}

function recordProfileStatus(stage, details = {}) {
  localStorage.setItem("fb_auto_profile_status", JSON.stringify({
    time: new Date().toISOString(), stage, ...details
  }));
}

function isOwnProfilePage(profileUrl) {
  const current = new URL(window.location.href);
  if (profileUrl.origin !== current.origin || profileUrl.pathname !== current.pathname) return false;
  if (profileUrl.pathname === "/profile.php") {
    const profileId = profileUrl.searchParams.get("id");
    return !!profileId && profileId === current.searchParams.get("id");
  }
  return true;
}

async function reportAccountProfile() {
  if (!autoConfig.account) return;
  try {
    recordProfileStatus("reading");
    const hint = navbarProfileHint();
    let displayName = hint.displayName;
    // /me is the signed-in user's own profile; the live page itself may show
    // another person's name and image, so never use its Open Graph metadata.
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch("/me", {
        credentials: "include", cache: "no-store", signal: controller.signal
      });
      recordProfileStatus("profile_page", { http: response.status, path: new URL(response.url).pathname });
      if (response.ok && !/\/(login|checkpoint)(\/|$)/i.test(new URL(response.url).pathname)) {
        const ownUrl = new URL(response.url);
        if (isOwnProfilePage(ownUrl)) {
          const visibleProfile = ownProfileMetadata(document);
          displayName = visibleProfile.displayName || displayName;
          recordProfileStatus("visible_profile", { hasName: !!displayName, hasHeading: !!document.querySelector('h1') });
        }
        if (!displayName) {
          const profilePage = new DOMParser().parseFromString(await response.text(), "text/html");
          const ownProfile = ownProfileMetadata(profilePage);
          displayName = ownProfile.displayName || displayName;
        }
      }
    } catch (error) {
      recordProfileStatus("profile_fetch_error", { error: String(error.message).slice(0, 120) });
      console.debug("Could not inspect /me profile page:", error.message);
    } finally {
      clearTimeout(timeout);
    }
    if (!displayName) {
      recordProfileStatus("identity_missing");
      return;
    }
    const signature = `${autoConfig.account}|${displayName}`;
    if (signature === lastReportedProfile) return;
    const saved = await fetch(PROFILE_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        account_id: autoConfig.account,
        display_name: displayName,
        avatar_url: ""
      })
    });
    recordProfileStatus("sent", { http: saved.status, hasName: !!displayName });
    if (saved.ok) lastReportedProfile = signature;
  } catch (error) {
    recordProfileStatus("bridge_error", { error: String(error.message).slice(0, 120) });
    console.debug("Facebook profile sync is not ready:", error.message);
  }
}

function findCommentBox() {
  const boxes = Array.from(document.querySelectorAll('[role="textbox"]'));
  return boxes.find((box) => {
    const label = [
      box.getAttribute("aria-label"),
      box.getAttribute("placeholder"),
      box.innerText
    ].filter(Boolean).join(" ");

    return /comment|留言|回覆|Write|Comment/i.test(label) || boxes.length === 1;
  });
}

function postComment(text) {
  const cleanText = String(text || "").trim();
  if (!cleanText || cleanText.toLowerCase() === "ignore" || cleanText === "[NO_REPLY]" || cleanText.startsWith("[ERROR")) {
    return false;
  }

  const box = findCommentBox();
  if (!box) {
    console.warn("Comment box not found yet");
    return false;
  }

  box.focus();
  document.execCommand("insertText", false, cleanText);

  setTimeout(() => {
    box.dispatchEvent(new KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      key: "Enter",
      code: "Enter",
      keyCode: 13,
      which: 13
    }));
    console.log("AI reply sent:", cleanText);
  }, COMMENT_DELAY);

  return true;
}

async function pollReplies() {
  if (isPolling) return;
  isPolling = true;

  try {
    if (!autoConfig.account || !autoConfig.jsonlFile) {
      console.warn("Missing FB auto-comment session config in URL");
      return;
    }

    const response = await fetch(`${API_BASE}/latest_reply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file_path: autoConfig.jsonlFile,
        stream_id: autoConfig.streamId,
        account_ids: [autoConfig.account]
      })
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const data = await response.json();
    for (const item of data.results || []) {
      if (item.stream_id !== autoConfig.streamId) continue;
      const accountResult = (item.account_results || []).find(
        (result) => result.account_id === autoConfig.account
      );
      if (!accountResult) continue;
      const reply = String(accountResult.reply || "").trim();
      const key = `${autoConfig.streamId}::${item.raw_text || item.input || ""}::${autoConfig.account}::${reply}`;

      if (!sentReplies.has(key) && postComment(reply)) {
        sentReplies.add(key);
      }
    }
  } catch (error) {
    console.warn("Local LLM bridge is not ready:", error.message);
  } finally {
    isPolling = false;
  }
}

setInterval(pollReplies, POLL_INTERVAL);
setTimeout(pollReplies, 1500);
