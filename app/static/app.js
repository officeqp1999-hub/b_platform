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

  // Карточка «Финансы» на обзоре: числа приходят отдельным запросом, чтобы
  // медленная или недоступная 1С не задерживала показ самой страницы.
  function money(n) {
    if (n === null || n === undefined) return "—";
    var neg = n < 0;
    var s = String(Math.round(Math.abs(n))).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    return (neg ? "-" : "") + s + " ₽";
  }

  function renderFinance(box, d) {
    box.textContent = "";
    if (d.status === "ok") {
      var row = document.createElement("div");
      row.className = "grid cols-3";
      [["выручка за месяц", d.revenue_month], ["деньги на счетах", d.cash_total], ["остатки на складах", d.stock_value]]
        .forEach(function (pair) {
          var cell = document.createElement("div");
          var val = document.createElement("div");
          val.className = "stat";
          val.textContent = money(pair[1]);
          var label = document.createElement("div");
          label.className = "stat-label";
          label.textContent = pair[0];
          cell.appendChild(val);
          cell.appendChild(label);
          row.appendChild(cell);
        });
      box.appendChild(row);
      return;
    }
    var p = document.createElement("p");
    var pill = document.createElement("span");
    pill.className = "pill s-" + d.status;
    pill.textContent = d.status === "warn" ? "Внимание" : d.status === "error" ? "Ошибка" : "Отключено";
    p.appendChild(pill);
    p.appendChild(document.createTextNode(" " + d.message));
    box.appendChild(p);
    if (d.action) {
      var ap = document.createElement("p");
      ap.className = "small muted mt-s";
      ap.textContent = "Что делать: " + d.action;
      box.appendChild(ap);
    }
  }

  var financeBox = document.querySelector("[data-finance-live]");
  if (financeBox) {
    fetch("/api/finance/summary", {credentials: "same-origin"})
      .then(function (r) {
        if (!r.ok) { throw new Error("http " + r.status); }
        return r.json();
      })
      .then(function (d) { renderFinance(financeBox, d); })
      .catch(function () {
        financeBox.textContent = "Не удалось получить данные. Обновите страницу (F5).";
      });
  }
})();
