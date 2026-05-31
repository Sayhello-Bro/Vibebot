console.log("FB Live Auto Comment bridge loaded");

const API_BASE = "http://127.0.0.1:5000";
const POLL_INTERVAL = 4000;
const COMMENT_DELAY = 800;

let isPolling = false;
const sentReplies = new Set();

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
  if (!cleanText || cleanText === "[NO_REPLY]" || cleanText.startsWith("[ERROR")) {
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
    const response = await fetch(`${API_BASE}/process`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const data = await response.json();
    for (const item of data.results || []) {
      const reply = String(item.reply || "").trim();
      const key = `${item.input || ""}::${reply}`;

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
