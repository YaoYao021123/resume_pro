// Service Worker — message relay between popup and content scripts

// Listen for messages from popup or content scripts
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'getServerUrl') {
    chrome.storage.local.get(['server_url'], (result) => {
      sendResponse({ url: result.server_url || 'http://localhost:8765' });
    });
    return true; // async response
  }

  if (message.action === 'apiRequest') {
    // Relay API requests from content scripts
    const { path, method, body } = message;
    chrome.storage.local.get(['server_url'], async (result) => {
      const baseUrl = result.server_url || 'http://localhost:8765';
      try {
        const resp = await fetch(`${baseUrl}${path}`, {
          method: method || 'GET',
          headers: { 'Content-Type': 'application/json' },
          body: body ? JSON.stringify(body) : undefined,
        });
        const data = await resp.json();
        sendResponse({ success: true, data });
      } catch (e) {
        sendResponse({ success: false, error: e.message });
      }
    });
    return true;
  }

  if (message.action === 'showBadge') {
    // Show badge on extension icon
    const { text, color } = message;
    if (sender.tab) {
      chrome.action.setBadgeText({ text: text || '', tabId: sender.tab.id });
      chrome.action.setBadgeBackgroundColor({ color: color || '#4A90D9', tabId: sender.tab.id });
    }
    sendResponse({ success: true });
    return false;
  }
});

// Initialize on install
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({
    server_url: 'http://localhost:8765',
    enabled: true,
  });
  console.log('[Resume Fill] Extension installed');
});

// Content script message handler for page detection
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url) {
    // Send a message to content script to initialize
    chrome.tabs.sendMessage(tabId, { action: 'pageLoaded' }).catch(() => {
      // Content script not yet loaded, ignore
    });
  }
});
