(function () {
  "use strict";

  /* ------------------------------------------------------------------ */
  /* Popover — single reusable div                                        */
  /* ------------------------------------------------------------------ */
  var popover = document.createElement("div");
  popover.className = "popover";
  var tooltipId = "reading-aid-tooltip";
  while (document.getElementById(tooltipId)) tooltipId += "-tip";
  popover.id = tooltipId;
  popover.setAttribute("role", "tooltip");
  popover.setAttribute("aria-live", "polite");
  popover.style.display = "none";
  document.body.appendChild(popover);

  var popoverTimer = null;

  function showPopover(text, targetEl) {
    clearTimeout(popoverTimer);
    popover.textContent = text;
    popover.style.display = "block";
    positionPopover(targetEl);
  }

  function hidePopover() {
    popoverTimer = setTimeout(function () {
      popover.style.display = "none";
    }, 80);
  }

  function positionPopover(el) {
    var rect = el.getBoundingClientRect();
    var scrollY = window.scrollY || document.documentElement.scrollTop;
    var scrollX = window.scrollX || document.documentElement.scrollLeft;
    var top = rect.bottom + scrollY + 6;
    var left = rect.left + scrollX;
    // Measure after showing; clamp both axes even on narrow screens.
    var width = popover.offsetWidth;
    var height = popover.offsetHeight;
    left = scrollX + Math.max(4, Math.min(rect.left, window.innerWidth - width - 4));
    top = scrollY + Math.max(4, Math.min(rect.bottom + 6, window.innerHeight - height - 4));
    popover.style.top = top + "px";
    popover.style.left = left + "px";
  }

  /* ------------------------------------------------------------------ */
  /* Term-def popovers                                                    */
  /* ------------------------------------------------------------------ */
  var termDefs = document.querySelectorAll(".term-def");
  for (var i = 0; i < termDefs.length; i++) {
    (function (el) {
      el.setAttribute("tabindex", "0");
      el.setAttribute("aria-describedby", tooltipId);
      var defText = el.getAttribute("data-def") || "";

      function onShow() { showPopover(defText, el); }
      function onHide() { hidePopover(); }

      el.addEventListener("mouseenter", onShow);
      el.addEventListener("mouseleave", onHide);
      el.addEventListener("focusin", onShow);
      el.addEventListener("focusout", onHide);
    })(termDefs[i]);
  }

  /* ------------------------------------------------------------------ */
  /* Citation popovers                                                    */
  /* ------------------------------------------------------------------ */
  var cites = document.querySelectorAll("cite[data-ref-id]");
  for (var j = 0; j < cites.length; j++) {
    (function (el) {
      el.setAttribute("tabindex", "0");
      el.setAttribute("aria-describedby", tooltipId);
      var refId = el.getAttribute("data-ref-id") || "";
      var refText = "";
      // Look up the reference text from the references section
      var refEl = document.getElementById(refId);
      if (refEl) {
        refText = refEl.textContent || refEl.innerText || "";
      } else {
        // Try to find a matching cite element in the references list
        var refCites = document.querySelectorAll("#references cite[data-ref-id]");
        for (var r = 0; r < refCites.length; r++) {
          if (refCites[r].getAttribute("data-ref-id") === refId) {
            refText = refCites[r].textContent || "";
            break;
          }
        }
      }

      if (!refText) refText = refId;

      function onShow() { showPopover(refText, el); }
      function onHide() { hidePopover(); }

      el.addEventListener("mouseenter", onShow);
      el.addEventListener("mouseleave", onHide);
      el.addEventListener("focusin", onShow);
      el.addEventListener("focusout", onHide);
    })(cites[j]);
  }

  /* ------------------------------------------------------------------ */
  /* Summary toggle — cycles skim → read → deep                          */
  /* ------------------------------------------------------------------ */
  var levels = ["skim", "read", "deep"];

  var toggleButtons = document.querySelectorAll(".summary-toggle");
  for (var k = 0; k < toggleButtons.length; k++) {
    (function (btn) {
      var block = btn.closest(".summary-block");
      if (!block) return;

      // Initialise to skim
      block.setAttribute("data-active-level", "skim");
      btn.textContent = "Summary: skim — click to expand";

      btn.addEventListener("click", function () {
        var current = block.getAttribute("data-active-level") || "skim";
        var idx = levels.indexOf(current);
        var next = levels[(idx + 1) % levels.length];
        block.setAttribute("data-active-level", next);
        btn.textContent = "Summary: " + next + " — click to expand";
        btn.setAttribute("aria-label", "Summary depth: " + next + ". Click to cycle.");
      });
    })(toggleButtons[k]);
  }

  /* ------------------------------------------------------------------ */
  /* Keyboard: Escape closes popover                                      */
  /* ------------------------------------------------------------------ */
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      popover.style.display = "none";
    }
  });
})();
