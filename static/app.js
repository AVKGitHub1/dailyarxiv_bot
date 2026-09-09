"use strict";

// Forms and paper abstracts work without JavaScript. Only job polling needs it.
if (document.querySelector('[data-job-running="true"]')) {
  const timer = window.setInterval(async () => {
    try {
      const response = await fetch("/status", { cache: "no-store" });
      if (!response.ok) return;
      const data = await response.json();
      if (!data.job || data.job.status !== "running") {
        window.clearInterval(timer);
        window.location.reload();
      }
    } catch {
      // A temporary network failure should not discard the visible page.
    }
  }, 3000);
}
