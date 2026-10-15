// Point the main download button at the visitor's OS, and make code blocks copyable.
(() => {
  const ua = navigator.userAgent;
  const platform = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "";
  const android = /Android/.test(ua);
  const ios = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  const os = android
    ? "android"
    : ios
      ? null
      : /win/i.test(platform) || /Windows/.test(ua)
        ? "windows"
        : /mac/i.test(platform) || /Mac OS X/.test(ua)
          ? "mac"
          : /linux/i.test(platform)
            ? "linux"
            : null;
  const names = { windows: "Windows", mac: "macOS", linux: "Linux", android: "Android" };
  const card = os && document.querySelector(`.dl[data-os="${os}"]`);

  if (card) {
    card.classList.add("is-yours");
    const button = document.getElementById("primary-download");
    button.href = card.href;
    document.getElementById("primary-download-label").textContent = `Download for ${names[os]}`;
    document.getElementById("primary-download-note").textContent =
      os === "mac" ? "Free · Apple silicon (Intel version below) · other platforms below" : "Free · no account · other platforms below";
  } else if (ios) {
    // No iOS app yet: iPhones and iPads join from Safari.
    document.getElementById("primary-download-label").textContent = "Get it for your other devices";
    document.getElementById("primary-download").href = "#download";
    document.getElementById("primary-download-note").textContent =
      "On iPhone and iPad nothing to install — scan the QR code another device shows.";
  }

  for (const button of document.querySelectorAll(".copy")) {
    button.addEventListener("click", async () => {
      const text = document.getElementById(button.dataset.copy).textContent;
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
      } catch {
        button.textContent = "Select & copy";
      }
      setTimeout(() => {
        button.textContent = "Copy";
      }, 1600);
    });
  }
})();
