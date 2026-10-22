// Open Transfer — system notifications while this window is in the background:
// someone wants to send you files, a transfer finished or failed, a clipboard
// arrived. Visible windows show toasts instead. Asked for with the bell button.

import { $, toast } from "./lib.js";

const supported = () => "Notification" in window;

/** Show a notification if allowed and the window is hidden; true if shown. */
export function notify(title, { body = "", tag = "", onClick } = {}) {
  if (!document.hidden) return false;
  const native = window.OpenTransferAndroid;
  if (native?.notify) {
    native.notify(title, body);
    return true;
  }
  if (!supported() || Notification.permission !== "granted") return false;
  try {
    const note = new Notification(title, { body, tag: tag || undefined, icon: "/static/icons/icon-192.png" });
    note.onclick = () => {
      window.focus();
      note.close?.();
      onClick?.();
    };
    return true;
  } catch {
    return false; // e.g. Chrome on Android needs a service worker for this
  }
}

export function initNotifications() {
  const button = $("#notify-button");
  if (!button) return;
  const update = () => {
    button.hidden = !supported() || window.OpenTransferAndroid?.notify || Notification.permission !== "default";
  };
  update();
  button.addEventListener("click", async () => {
    let result = "denied";
    try {
      result = await Notification.requestPermission();
    } catch {
      /* treated as denied */
    }
    update();
    toast(result === "granted" ? "Notifications are on" : "Notifications are off — you can allow them in the browser’s site settings", {
      icon: "bell",
      tone: result === "granted" ? "success" : "info",
    });
  });
}
