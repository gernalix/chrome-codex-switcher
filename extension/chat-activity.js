/* Observe only the browser's visible generation control, never chat contents. */
(() => {
  if (window.__ccsActivityObserver) return;
  window.__ccsActivityObserver = true;

  let latestUrl = "";
  let latestStatus = "";
  let lastSent = 0;
  let pendingIdle = 0;
  let timer = null;

  const threadUrl = () => {
    if (location.origin !== "https://chatgpt.com") return "";
    if (!/^\/(?:c\/|g\/[^/]+\/c\/)/.test(location.pathname)) return "";
    return location.origin + location.pathname;
  };

  const generating = () => Boolean(document.querySelector([
    'button[data-testid="stop-button"]',
    'button[aria-label="Stop"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Stop streaming"]',
    'button[aria-label="Arresta la generazione"]',
    'button[aria-label="Interrompi la generazione"]',
    'button[aria-label="Stop response"]'
  ].join(",")));

  function report() {
    const url = threadUrl();
    if (!url) return;
    const now = Date.now();
    const status = generating() ? "running" : "idle";
    if (status === "idle" && latestStatus === "running") {
      // Avoid transient disappearance during a React re-render.
      if (!pendingIdle) pendingIdle = now;
      if (now - pendingIdle < 2500) {
        schedule();
        return;
      }
    } else {
      pendingIdle = 0;
    }
    if (url === latestUrl && status === latestStatus && now - lastSent < 15000) return;
    chrome.runtime.sendMessage({
      type: "activity:state", url, title: document.title, status
    }, response => {
      if (chrome.runtime.lastError || !response?.ok) return;
      latestUrl = url;
      latestStatus = status;
      lastSent = Date.now();
    });
  }

  function schedule() {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {timer = null; report();}, 350);
  }

  const observer = new MutationObserver(schedule);
  observer.observe(document.documentElement, {
    subtree: true, childList: true, attributes: true,
    attributeFilter: ["aria-label", "data-testid", "hidden"]
  });
  const heart = setInterval(report, 15000);
  window.addEventListener("popstate", schedule);
  window.addEventListener("pagehide", () => {
    observer.disconnect();
    clearInterval(heart);
    clearTimeout(timer);
  }, {once: true});
  report();
})();
