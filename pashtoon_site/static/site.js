/* Basket (kept in this browser), menu buttons, checkout totals and the chat widget. No framework. */
(function () {
  const PF = (window.PF = {});
  const KEY = "pf_cart";
  const money = (n) => "£" + Number(n || 0).toFixed(2);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  PF.cart = function () {
    try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { return []; }
  };
  PF.save = function (c) {
    try { localStorage.setItem(KEY, JSON.stringify(c.filter((x) => x.qty > 0))); } catch (e) {}
    PF.updateCount();
  };
  PF.setQty = function (opt, qty) {
    const c = PF.cart();
    const id = Number(opt.dataset.option);
    let line = c.find((x) => x.option_id === id);
    if (!line) { line = { option_id: id, qty: 0, name: opt.dataset.name, portion: opt.dataset.portion, price: Number(opt.dataset.price) }; c.push(line); }
    line.qty = Math.max(0, Math.min(50, qty));
    PF.save(c);
    PF.paintOption(opt);
  };
  PF.updateCount = function () {
    const c = PF.cart();
    const n = c.reduce((a, x) => a + x.qty, 0);
    const t = c.reduce((a, x) => a + x.qty * x.price, 0);
    document.querySelectorAll("[data-cart-count]").forEach((el) => (el.textContent = n));
    document.querySelectorAll("[data-cart-total]").forEach((el) => (el.textContent = money(t)));
    const bar = document.getElementById("basket-bar");
    if (bar) { bar.classList.toggle("hide", n === 0); document.body.classList.toggle("has-basket", n > 0); }
  };
  PF.paintOption = function (opt) {
    const line = PF.cart().find((x) => x.option_id === Number(opt.dataset.option));
    const q = line ? line.qty : 0;
    opt.querySelector("[data-add]").classList.toggle("hide", q > 0);
    opt.querySelector(".qty").classList.toggle("hide", q === 0);
    opt.querySelector("[data-q]").textContent = q;
  };

  // menu page
  document.querySelectorAll(".opt[data-option]").forEach((opt) => {
    PF.paintOption(opt);
    opt.querySelector("[data-add]").addEventListener("click", () => PF.setQty(opt, 1));
    opt.querySelector("[data-inc]").addEventListener("click", () => PF.setQty(opt, Number(opt.querySelector("[data-q]").textContent) + 1));
    opt.querySelector("[data-dec]").addEventListener("click", () => PF.setQty(opt, Number(opt.querySelector("[data-q]").textContent) - 1));
  });
  const tabs = document.querySelectorAll("#tabs a");
  if (tabs.length && "IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) tabs.forEach((t) => t.classList.toggle("on", t.dataset.tab === e.target.dataset.section));
      });
    }, { rootMargin: "-40% 0px -55% 0px" });
    document.querySelectorAll("[data-section]").forEach((s) => io.observe(s));
  }
  PF.updateCount();

  // checkout page
  PF.checkout = function () {
    const form = document.getElementById("checkout");
    const cart = PF.cart();
    if (!cart.length) { form.classList.add("hide"); document.getElementById("empty").classList.remove("hide"); return; }
    document.getElementById("cart-field").value = JSON.stringify(cart.map((x) => ({ option_id: x.option_id, qty: x.qty })));
    const lines = document.getElementById("lines");
    function paintLines() {
      const c = PF.cart();
      lines.innerHTML = c.map((x) =>
        `<div class="summary-line" data-id="${x.option_id}"><span>${esc(x.name)} <span class="faint small">(${esc(x.portion)})</span><br>
         <span class="qty" style="margin-top:6px"><button type="button" data-d>−</button><span>${x.qty}</span><button type="button" data-i>+</button></span></span>
         <span>${money(x.qty * x.price)}</span></div>`).join("");
      lines.querySelectorAll("[data-id]").forEach((row) => {
        const id = Number(row.dataset.id);
        const change = (d) => { const c2 = PF.cart(); const l = c2.find((y) => y.option_id === id); l.qty += d; PF.save(c2);
          if (!PF.cart().length) { location.reload(); return; }
          document.getElementById("cart-field").value = JSON.stringify(PF.cart().map((y) => ({ option_id: y.option_id, qty: y.qty })));
          paintLines(); requote(); };
        row.querySelector("[data-d]").onclick = () => change(-1);
        row.querySelector("[data-i]").onclick = () => change(1);
      });
    }
    const delivery = () => (form.querySelector("input[name=fulfilment]:checked") || {}).value === "delivery";
    function toggleDelivery() {
      document.getElementById("delivery-fields").classList.toggle("hide", !delivery());
      document.getElementById("collect-note").classList.toggle("hide", delivery());
      document.getElementById("address").required = delivery();
      document.getElementById("postcode").required = delivery();
    }
    let timer;
    function requote() {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const body = { items: PF.cart().map((x) => ({ option_id: x.option_id, qty: x.qty })), fulfilment: delivery() ? "delivery" : "collection",
          postcode: document.getElementById("postcode").value, date: document.getElementById("date").value, time: document.getElementById("time").value };
        const errBox = document.getElementById("quote-error");
        if (delivery() && body.postcode.replace(/\s/g, "").length < 5) {
          body.fulfilment = "collection";
        }
        try {
          const r = await fetch("/api/quote", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF": window.CSRF }, body: JSON.stringify(body) });
          const q = await r.json();
          if (q.ok) {
            document.getElementById("food").textContent = money(q.food_total);
            document.getElementById("fee").textContent = delivery() && body.fulfilment === "collection" ? "enter postcode" : money(q.delivery_fee);
            document.getElementById("total").textContent = money(q.total);
            errBox.classList.add("hide");
          } else { errBox.textContent = q.error; errBox.classList.remove("hide"); }
        } catch (e) {}
      }, 350);
    }
    paintLines(); toggleDelivery(); requote();
    form.querySelectorAll("input[name=fulfilment]").forEach((r) => r.addEventListener("change", () => { toggleDelivery(); requote(); }));
    ["postcode", "date", "time"].forEach((id) => document.getElementById(id).addEventListener("change", requote));
    document.getElementById("postcode").addEventListener("input", requote);
  };

  // chat widget
  const box = document.getElementById("chat-box");
  if (box) {
    const log = document.getElementById("chat-log");
    const input = document.getElementById("chat-input");
    let last = 0, poller = null, sending = false;
    const label = { ai: window.ASSISTANT + " · AI assistant", owner: "The Pashtoon Flavours (owner)", customer: "You" };
    function add(l) {
      if (l.id && l.id <= last) return;
      if (l.id) last = l.id;
      if (l.sender === "customer" && document.querySelector(`[data-pending]`)) { document.querySelector("[data-pending]").remove(); }
      const d = document.createElement("div");
      d.className = "bubble " + l.sender;
      d.innerHTML = (l.sender !== "customer" ? `<span class="who">${esc(label[l.sender] || "")}</span>` : "") + esc(l.text);
      log.appendChild(d);
      log.scrollTop = log.scrollHeight;
    }
    async function poll() {
      try { const r = await fetch("/api/chat/poll?after=" + last); const j = await r.json(); (j.lines || []).forEach(add); } catch (e) {}
    }
    PF.openChat = function (prefill) {
      box.classList.remove("hide");
      document.getElementById("chat-open").classList.add("hide");
      if (prefill) input.value = prefill;
      input.focus();
      poll();
      if (!poller) poller = setInterval(poll, 6000);
    };
    document.getElementById("chat-open").onclick = () => PF.openChat();
    document.getElementById("chat-close").onclick = () => { box.classList.add("hide"); document.getElementById("chat-open").classList.remove("hide"); clearInterval(poller); poller = null; };
    document.getElementById("chat-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text || sending) return;
      sending = true; input.value = "";
      const mine = document.createElement("div"); mine.className = "bubble me"; mine.dataset.pending = "1"; mine.textContent = text; log.appendChild(mine);
      const typing = document.createElement("div"); typing.className = "typing"; typing.textContent = window.ASSISTANT + " is typing…"; log.appendChild(typing);
      log.scrollTop = log.scrollHeight;
      try {
        const r = await fetch("/api/chat/send", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF": window.CSRF }, body: JSON.stringify({ text, after: last }) });
        const j = await r.json();
        typing.remove();
        if (j.ok) j.lines.forEach(add); else { mine.remove(); add({ sender: "ai", text: j.error || "Sorry, that didn't send." }); input.value = text; }
      } catch (err) { typing.remove(); add({ sender: "ai", text: "Sorry, we couldn't send that. Please check your connection and try again." }); input.value = text; }
      sending = false;
    });
    try { if (sessionStorage.getItem("pf_chat_open")) PF.openChat(); } catch (e) {}
    window.addEventListener("beforeunload", () => { try { box.classList.contains("hide") ? sessionStorage.removeItem("pf_chat_open") : sessionStorage.setItem("pf_chat_open", "1"); } catch (e) {} });
  }
})();
