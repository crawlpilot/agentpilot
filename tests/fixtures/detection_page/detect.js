/*
 * Detection checks for the P0 stealth regression fixture.
 *
 * Results are written to the DOM (`#agentpilot-detection-result`'s `textContent`,
 * as JSON) rather than a `window` global. This isn't stylistic: Patchright's
 * `page.evaluate()` runs in a JS world isolated from the page's own <script>
 * execution context (empirically confirmed -- a global this script sets on
 * `window` is invisible to `page.evaluate("window.x")`, but a DOM attribute
 * it writes IS visible, since the DOM is shared across worlds even when JS
 * globals aren't). A real third-party anti-bot script embedded in a page
 * would run in this same real page world regardless, so the checks
 * themselves are exactly what such a script would see -- only the
 * *retrieval* mechanism has to route through the DOM.
 *
 * `navigator.webdriver` and `window.chrome` are straightforward feature
 * checks. The `Runtime.enable` check is a timing side-channel, empirically
 * validated against this repo's Patchright/Chrome build (see
 * docs/capacity-planning.md "Spike findings"): a CDP client with the Runtime
 * domain enabled forces V8 to build+send a `Runtime.consoleAPICalled`
 * protocol event on every `console.*` call, which is measurably slower even
 * if nothing ever reads the event. 20000 console.log calls took ~25-31ms
 * with no CDP Runtime listener attached and ~100-145ms with one attached
 * (measured both standalone and on this exact fixture page), confirmed
 * reproducible across repeated runs -- a >2x gap, so the 70ms threshold
 * below sits with a wide margin in both directions for this reference
 * container. (An earlier candidate check -- a getter on a logged object,
 * expecting console.log's object-preview machinery to eagerly invoke it --
 * was tested and found NOT to fire on current Chrome; that specific leak
 * appears to have been patched upstream, so it's omitted here rather than
 * shipped as a check that would always silently "pass".)
 *
 * The page/screen coordinate check targets crbug#1477537 (the bug
 * `cdp_patches` exists to work around): CDP-synthesized pointer events can
 * produce a `screenX/screenY` inconsistent with `clientX/clientY` plus the
 * window's chrome offset. P0 never dispatches synthetic input (that's P1's
 * interaction verbs), so this stays "no mismatch" until a real event fires --
 * which is correct: nothing to detect yet, not a false pass.
 */

(function () {
  var state = {
    webdriver: navigator.webdriver === undefined ? null : navigator.webdriver,
    hasChromeObject: typeof window.chrome === "object" && window.chrome !== null,
    runtimeEnableSuspected: null,
    consoleLogMs: null,
    coordinateMismatch: false,
  };

  function resultElement() {
    return document.getElementById("agentpilot-detection-result");
  }

  function publish() {
    resultElement().textContent = JSON.stringify(state);
  }

  function runTimingCheck() {
    var N = 20000;
    var t0 = performance.now();
    for (var i = 0; i < N; i++) {
      console.log("__agentpilot_probe", i);
    }
    var elapsed = performance.now() - t0;
    state.consoleLogMs = elapsed;
    state.runtimeEnableSuspected = elapsed > 70;
  }

  function checkPointerCoordinates(e) {
    var chromeOffset = window.outerWidth - window.innerWidth;
    var expectedScreenX = e.clientX + window.screenX + chromeOffset;
    if (Math.abs(e.screenX - expectedScreenX) > 5) {
      state.coordinateMismatch = true;
      publish();
    }
  }

  window.addEventListener("mousemove", checkPointerCoordinates, false);
  window.addEventListener("click", checkPointerCoordinates, false);

  runTimingCheck();
  publish();
})();
