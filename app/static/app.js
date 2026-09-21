/* Минимум скриптов: подтверждения, «идёт проверка», копирование. Без библиотек. */
(function () {
  "use strict";

  document.addEventListener("submit", function (e) {
    var form = e.target;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      e.preventDefault();
      return;
    }
    var busyText = form.getAttribute("data-busy");
    if (busyText) {
      form.classList.add("busy-on");
      var buttons = form.querySelectorAll("button[type=submit], .btn[type=submit]");
      // не отключаем кнопки сразу — иначе их name/value не попадут в форму
      window.setTimeout(function () {
        buttons.forEach(function (b) {
          b.classList.add("busy");
          if (b.hasAttribute("data-busy-label")) {
            b.textContent = b.getAttribute("data-busy-label");
          }
        });
      }, 0);
    }
  });

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var target = document.querySelector(btn.getAttribute("data-copy"));
    if (!target) return;
    target.select();
    var done = function () {
      var old = btn.textContent;
      btn.textContent = "Скопировано";
      window.setTimeout(function () { btn.textContent = old; }, 1500);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(target.value).then(done, function () { document.execCommand("copy"); done(); });
    } else {
      document.execCommand("copy");
      done();
    }
  });
})();
