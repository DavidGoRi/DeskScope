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

  // Knowledge base suggestions while typing a new ticket's summary.
  // Results are inserted with textContent only, never as HTML.
  document.querySelectorAll("[data-suggest-for]").forEach(function (box) {
    var input = document.querySelector('[name="' + box.getAttribute("data-suggest-for") + '"]');
    var list = box.querySelector("ul");
    if (!input || !list || !window.fetch) return;
    var timer = null;
    var last = "";

    function show(items) {
      list.textContent = "";
      items.forEach(function (item) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.href = "/kb/" + encodeURIComponent(item.id);
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = item.title;
        li.appendChild(a);
        list.appendChild(li);
      });
      box.hidden = items.length === 0;
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var q = input.value.trim();
        if (q === last) return;
        last = q;
        if (q.length < 4) { show([]); return; }
        fetch("/kb/suggest?q=" + encodeURIComponent(q), { credentials: "same-origin" })
          .then(function (r) { return r.ok ? r.json() : []; })
          .then(function (items) { if (input.value.trim() === q) show(items); })
          .catch(function () { show([]); });
      }, 300);
    });
  });
})();
