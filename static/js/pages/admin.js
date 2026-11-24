// People in the app: the administrator's list (block, unblock, delete), invitations, and sign-up by an invitation link.
import { $, api, ago, confirmBox, date, emptyBox, errorBox, esc, go, icon, loadSession, modal, route, state, toast, usd } from "../core.js";
import { brand, header, needLogin } from "../layout.js";

// ------------------------------------------------------------------ sign-up by invitation: /join?invite=...
export async function joinPage(main) {
  const token = route().q.get("invite") || "";
  if (state.user) {
    main.innerHTML = `<div class="gate"><span class="gate-ic">${icon("user", 22)}</span><h2>Вы уже вошли как ${esc(state.user.username)}</h2>
      <p>Чтобы создать другой аккаунт по приглашению, сначала выйдите.</p><a class="btn btn-primary" href="/">На главную</a></div>`;
    return;
  }
  main.innerHTML = '<div class="sk-block tall"></div>';
  try {
    await api("/api/auth/invite?token=" + encodeURIComponent(token));
  } catch (e) {
    main.innerHTML = `<div class="gate"><span class="gate-ic">${icon("alert", 22)}</span><h2>Приглашение не работает</h2>
      <p>${esc(e.message)}. Попросите администратора прислать новую ссылку.</p><button class="btn" data-login>У меня есть аккаунт — войти</button></div>`;
    return;
  }
  main.innerHTML = `<div class="auth-page"><div class="auth-card">${brand()}
    <h1>Регистрация по приглашению</h1>
    <p class="muted">Придумайте логин и пароль. На ваш виртуальный счёт будет зачислено $1 000 — все сделки виртуальные, по живым ценам Polymarket.</p>
    <form class="form" id="join-form">
      <label>Логин<input name="username" autocomplete="username" minlength="3" maxlength="40" required placeholder="латиница, цифры, _ . @ -"></label>
      <label>Пароль<input name="password" type="password" autocomplete="new-password" minlength="8" required placeholder="не короче 8 символов"></label>
      <label>Повторите пароль<input name="again" type="password" autocomplete="new-password" minlength="8" required></label>
      <p class="form-error" hidden></p>
      <button class="btn btn-primary btn-block">Создать аккаунт и войти</button>
    </form></div></div>`;
  const f = $("#join-form", main);
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    const err = $(".form-error", f);
    const fail = (m) => {
      err.textContent = m;
      err.hidden = false;
    };
    if (f.password.value !== f.again.value) return fail("Пароли не совпадают");
    try {
      await api("/api/auth/register", { invite: token, username: f.username.value, password: f.password.value });
      await loadSession();
      header();
      toast("Аккаунт создан. Добро пожаловать!");
      go("/", { replace: true });
    } catch (e) {
      fail(e.message);
    }
  };
}

// ------------------------------------------------------------------ administrator: /admin
export async function adminPage(main) {
  if (!state.user) return needLogin(main, "Пользователи", "Войдите как администратор.");
  if (!state.user.is_admin) {
    main.innerHTML = `<div class="gate"><span class="gate-ic">${icon("shield", 22)}</span><h2>Только для администратора</h2><p>Этот раздел видит только администратор приложения.</p><a class="btn btn-primary" href="/">На главную</a></div>`;
    return;
  }
  main.innerHTML = `<div class="page-head"><h1>Пользователи</h1>
      <p class="muted">Новые люди попадают в приложение только по приглашению: одноразовая ссылка с ограниченным сроком.
      У каждого свой виртуальный счёт, копирование и автопилот.</p></div>
    <section class="card"><div class="card-head"><h3>Приглашения</h3><button class="btn btn-primary btn-sm" id="inv-new">${icon("plus", 14)} Пригласить</button></div>
      <div id="inv-list"><div class="sk-block"></div></div></section>
    <section class="card adm-users"><div class="card-head"><h3>Все пользователи</h3></div><div id="usr-list"><div class="sk-block tall"></div></div></section>`;
  $("#inv-new").onclick = () => inviteDialog(() => adminPage(main));
  main.onclick = (e) => onAction(e, main);
  loadInvites();
  loadUsers();
}

const STATUS = { open: ["Ждёт", "badge-green"], used: ["Использовано", "badge-gray"], expired: ["Истекло", "badge-gray"], revoked: ["Отозвано", "badge-red"] };

async function loadInvites() {
  const box = $("#inv-list");
  if (!box) return;
  try {
    const rows = await api("/api/admin/invites");
    box.innerHTML = rows.length ? `<div class="adm-list">${rows.map((r) => `<div class="adm-row">
        <div class="adm-main"><b>${esc(r.note || "Приглашение №" + r.id)}</b>
          <small>создано ${date(r.created)} · ${r.status === "used" ? `зарегистрировался <b>${esc(r.used_by_name || "—")}</b> ${ago(r.used_at)}`
            : r.status === "open" ? `действует до ${date(r.expires)}` : ""}</small></div>
        <span class="badge ${STATUS[r.status][1]}">${STATUS[r.status][0]}</span>
        ${r.status === "open" ? `<button class="btn btn-sm btn-danger-ghost" data-revoke="${r.id}">Отозвать</button>` : ""}</div>`).join("")}</div>`
      : `<p class="muted small">Приглашений пока нет. Нажмите «Пригласить» и отправьте ссылку человеку.</p>`;
  } catch (e) {
    box.innerHTML = errorBox(e, false);
  }
}

async function loadUsers() {
  const box = $("#usr-list");
  if (!box) return;
  try {
    const rows = await api("/api/admin/users");
    box.innerHTML = rows.length ? `<div class="adm-list">${rows.map((u) => `<div class="adm-row ${u.blocked ? "is-blocked" : ""}">
        <div class="adm-main"><b>${esc(u.username)}</b> ${u.is_admin ? '<span class="badge badge-gold">Админ</span>' : ""}${u.me ? ' <span class="badge badge-gray">Это вы</span>' : ""}${u.blocked ? ' <span class="badge badge-red">Заблокирован</span>' : ""}
          <small>с ${date(u.created)} · ${u.last_login ? "вход " + ago(u.last_login) : "ещё не входил"} · копий ${u.copies} · автопилот ${u.autopilot ? "включён" : "выключен"}</small></div>
        <div class="adm-num"><b>${usd(u.equity)}</b><small>всего на счетах</small></div>
        <div class="adm-acts">${u.me || u.is_admin ? "" : `<button class="btn btn-sm" data-block="${u.id}" data-on="${u.blocked ? 0 : 1}" data-name="${esc(u.username)}">${icon(u.blocked ? "check" : "ban", 14)} ${u.blocked ? "Разблокировать" : "Заблокировать"}</button>
          <button class="btn btn-sm btn-danger-ghost" data-delete="${u.id}" data-name="${esc(u.username)}" title="Удалить со всеми данными">${icon("trash", 14)}</button>`}</div></div>`).join("")}</div>`
      : emptyBox("Пользователей нет");
  } catch (e) {
    box.innerHTML = errorBox(e, false);
  }
}

async function onAction(e, main) {
  const rv = e.target.closest("[data-revoke]");
  const bl = e.target.closest("[data-block]");
  const del = e.target.closest("[data-delete]");
  try {
    if (rv) {
      if (!(await confirmBox("Отозвать приглашение?", "Ссылка перестанет работать.", "Отозвать", true))) return;
      await api("/api/admin/invite/revoke", { id: Number(rv.dataset.revoke) });
      toast("Приглашение отозвано");
      loadInvites();
    } else if (bl) {
      const on = bl.dataset.on === "1";
      if (on && !(await confirmBox(`Заблокировать ${bl.dataset.name}?`, "Человек не сможет войти, его копирование и автопилот остановятся. Данные сохранятся — блокировку можно снять.", "Заблокировать", true))) return;
      await api("/api/admin/user/block", { id: Number(bl.dataset.block), blocked: on });
      toast(on ? "Пользователь заблокирован" : "Блокировка снята");
      loadUsers();
    } else if (del) {
      if (!(await confirmBox(`Удалить ${del.dataset.name}?`, "Будут удалены его счета, позиции, заявки, копирование, автопилот, избранное и уведомления. Отменить нельзя.", "Удалить навсегда", true))) return;
      await api("/api/admin/user/delete", { id: Number(del.dataset.delete) });
      toast("Пользователь удалён");
      loadUsers();
    }
  } catch (err) {
    toast(err.message, "bad");
  }
}

function inviteDialog(done) {
  const d = modal("Пригласить человека", `<form class="form" id="inv-form">
      <label>Для кого (необязательно)<input name="note" maxlength="80" placeholder="например, Андрей"></label>
      <label>Ссылка действует<select name="days"><option value="1">1 день</option><option value="3">3 дня</option><option value="7" selected>7 дней</option><option value="30">30 дней</option></select></label>
      <p class="form-error" hidden></p>
      <button class="btn btn-primary btn-block">Создать ссылку</button></form>`);
  const f = $("#inv-form", d);
  f.onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      const r = await api("/api/admin/invite", { note: f.note.value, days: Number(f.days.value) });
      const link = `${location.origin}/join?invite=${encodeURIComponent(r.token)}`;
      f.outerHTML = `<div class="form"><p>Отправьте эту ссылку человеку. Она сработает <b>один раз</b> и действует до ${date(r.expires)}.
          Сохранить её потом будет нельзя: в базе хранится только отпечаток.</p>
        <div class="inv-link"><input readonly value="${esc(link)}" id="inv-url" aria-label="Ссылка-приглашение"><button class="btn btn-primary" id="inv-copy">${icon("copy", 14)} Скопировать</button></div></div>`;
      const url = $("#inv-url", d);
      url.select();
      $("#inv-copy", d).onclick = async () => {
        try {
          await navigator.clipboard.writeText(link);
        } catch {
          url.select();
          document.execCommand("copy");
        }
        toast("Ссылка скопирована");
      };
      done();
    } catch (err) {
      const el = $(".form-error", f);
      el.textContent = err.message;
      el.hidden = false;
    }
  };
}
