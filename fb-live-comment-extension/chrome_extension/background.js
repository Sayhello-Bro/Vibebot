chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "export_facebook_cookies") return false;
  chrome.cookies.getAll({ domain: "facebook.com" }, (cookies) => {
    const error = chrome.runtime.lastError;
    if (error) {
      sendResponse({ ok: false, error: error.message });
      return;
    }
    sendResponse({ ok: true, cookies });
  });
  return true;
});
