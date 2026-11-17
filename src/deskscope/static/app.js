// Live preview of the ITIL priority while choosing impact and urgency.
// Progressive enhancement: the server computes the real priority anyway.
(function () {
  "use strict";
  document.querySelectorAll("form[data-matrix]").forEach(function (form) {
    var matrix = JSON.parse(form.getAttribute("data-matrix"));
    var impact = form.querySelector("[name=impact]");
    var urgency = form.querySelector("[name=urgency]");
    var out = form.querySelector("[data-priority-preview]");
    if (!impact || !urgency || !out) return;

    function update() {
      var priority = matrix[impact.value + "|" + urgency.value];
      var label = form.querySelector('[data-label="' + priority + '"]');
      out.textContent = label ? label.textContent : priority;
      out.className = "prio prio-" + priority;
    }

    impact.addEventListener("change", update);
    urgency.addEventListener("change", update);
    update();
  });
})();
