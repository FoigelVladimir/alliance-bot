import { requestJson } from "alliance/api";

export function attachDraftEditor(form, { beginMutation = () => () => {} } = {}) {
  const editor = form.elements.text;
  const status = form.querySelector("[data-draft-status]");
  const errorBox = form.querySelector("[data-command-error]");
  const submit = form.querySelector("button[type=submit]");
  const refresh = form.querySelector("[data-draft-refresh]");
  const comparison = form.querySelector("[data-draft-comparison]");
  const serverEditor = comparison.querySelector("textarea");
  const csrfToken = form.elements.csrfmiddlewaretoken.value;
  const assignment = Number(form.elements.expected_assignment_version.value);

  let savedText = editor.value;
  let revision = form.elements.draft_revision.value;
  let draftId = form.elements.draft_id.value || null;
  let pending, timer, serverCopy, sendBody;
  let accepted = null;
  let paused = false;
  let sending = false;
  let lostAccess = false;
  const dirty = () => editor.value !== savedText;

  function blockEditor() {
    clearTimeout(timer);
    lostAccess = true;
    paused = true;
    editor.readOnly = true;
    submit.disabled = true;
    refresh.hidden = true;
    comparison.hidden = true;
    status.textContent = "Редактирование недоступно. Скопируй нужный текст перед обновлением страницы.";
  }

  function showComparison(state) {
    if (!state.editable || state.assignment_version !== assignment) {
      blockEditor();
      return;
    }
    serverCopy = state.draft;
    serverEditor.value = serverCopy?.text ?? "";
    comparison.hidden = false;
    refresh.hidden = true;
    status.textContent = "Сравни свой текст выше с версией на сервере.";
  }

  async function readServerCopy() {
    comparison.hidden = true;
    refresh.disabled = true;
    try {
      showComparison(await requestJson(form.dataset.draftUrl));
    } catch (error) {
      if ([401, 403, 404].includes(error.status)) blockEditor();
      else status.textContent = "Не удалось прочитать версию с сервера. Попробуй ещё раз.";
    } finally {
      refresh.disabled = false;
    }
  }

  function pause() {
    paused = true;
    refresh.hidden = false;
    status.textContent = "Сохранение приостановлено. Перечитываем версию с сервера.";
    void readServerCopy();
  }

  function schedule() {
    clearTimeout(timer);
    if (!paused && !sending && !pending && dirty()) timer = setTimeout(() => void save(), 600);
  }

  async function write(text) {
    status.textContent = "Сохраняем черновик…";
    try {
      const { draft } = await requestJson(form.dataset.draftUrl, {
        method: "POST", csrfToken,
        body: { expected_assignment_version: assignment, expected_draft_revision: revision, text },
      });
      draftId = draft.id;
      revision = draft.revision;
      savedText = draft.text;
      status.textContent = dirty() ? "Есть несохранённые изменения" : "Черновик сохранён";
    } catch (error) {
      const code = error.payload?.error?.code;
      if ([401, 403, 404].includes(error.status) || code === "assignment_version_conflict") blockEditor();
      else if (code === "draft_too_long") {
        status.textContent = "Сократи черновик до 20 000 символов.";
      } else pause();
    }
  }

  function save() {
    clearTimeout(timer);
    if (pending || paused || !dirty()) return pending;
    const text = editor.value;
    pending = write(text).finally(() => {
      pending = null;
      if (editor.value !== text) schedule();
    });
    return pending;
  }

  function chooseServerCopy(keepLocal) {
    draftId = serverCopy?.id ?? null;
    revision = serverCopy?.revision ?? "0";
    savedText = serverCopy?.text ?? "";
    if (!keepLocal) editor.value = savedText;
    comparison.hidden = true;
    paused = false;
    status.textContent = dirty() ? "Есть несохранённые изменения" : "Черновик совпадает с сервером";
    if (keepLocal) void save();
  }

  editor.addEventListener("input", () => {
    if (accepted && editor.value !== accepted.text) {
      form.elements.client_operation_id.value = crypto.randomUUID();
      accepted = null;
    }
    if (!paused) status.textContent = dirty() ? "Есть несохранённые изменения" : "Черновик сохранён";
    schedule();
  });
  refresh.addEventListener("click", readServerCopy);
  comparison.querySelector("[data-draft-use-server]").addEventListener("click", () => chooseServerCopy(false));
  comparison.querySelector("[data-draft-use-local]").addEventListener("click", () => chooseServerCopy(true));

  window.addEventListener("beforeunload", (event) => {
    if (dirty() || pending || sending || sendBody) {
      event.preventDefault();
      event.returnValue = true;
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (sending || lostAccess) return;
    if (!sendBody && (!editor.value.trim() || Array.from(editor.value).length > 9000)) {
      errorBox.textContent = "Введите текст ответа до 9000 символов.";
      return;
    }
    if (accepted && editor.value === accepted.text) {
      errorBox.textContent = "Этот ответ уже поставлен в очередь. Проверь его состояние в истории.";
      return;
    }
    sending = true;
    submit.disabled = true;
    editor.readOnly = true;
    errorBox.textContent = "";
    let release;
    let success = false;
    try {
      if (!sendBody) {
        await pending;
        await save();
        if (paused || dirty()) {
          errorBox.textContent = "Сначала сохрани черновик.";
          return;
        }
        sendBody = {
          client_operation_id: form.elements.client_operation_id.value,
          expected_assignment_version: assignment,
          text: editor.value,
          source_draft: draftId ? { id: draftId, revision } : null,
        };
      }
      release = beginMutation();
      const result = await requestJson(form.action, { method: "POST", body: sendBody, csrfToken });
      accepted = {id: result.response.id, text: sendBody.text, revision};
      status.textContent = "Ответ поставлен в очередь. Состояние видно в истории.";
      sendBody = null;
      success = true;
    } catch (error) {
      const code = error.payload?.error?.code;
      if (error.status >= 400 && error.status < 500) {
        sendBody = null;
        if ([401, 403, 404].includes(error.status) || code === "assignment_version_conflict") blockEditor();
        else if (code === "draft_revision_conflict") pause();
        errorBox.textContent = error.payload?.error?.message || "Не удалось поставить ответ в очередь.";
      } else {
        errorBox.textContent = "Подтверждение не пришло. Повтори тот же запрос, чтобы узнать его результат.";
      }
    } finally {
      sending = false;
      submit.disabled = lostAccess;
      editor.readOnly = lostAccess || Boolean(sendBody);
      release?.(success);
    }
  });
  async function ensureSaved() {
    if (sending || lostAccess) return false;
    await pending;
    await save();
    return !paused && !dirty();
  }

  async function observeHistory(history, readDraft) {
    const sent = accepted;
    if (!sent || dirty() || paused || lostAccess || pending || sending || revision !== sent.revision) return;
    const confirmed = Array.from(history.querySelectorAll("[data-history-key]"))
      .some((item) => item.dataset.historyKey === `1:${sent.id}` && item.dataset.historyStatus === "sent");
    if (!confirmed) return;
    const state = await readDraft(form.dataset.draftUrl);
    if (accepted !== sent || dirty() || paused || lostAccess || pending || sending) return;
    if (!state.editable || state.assignment_version !== assignment) {
      blockEditor();
    } else if (state.draft?.text === "" && BigInt(state.draft.revision) === BigInt(sent.revision) + 1n) {
      serverCopy = state.draft;
      chooseServerCopy(false);
      form.elements.client_operation_id.value = crypto.randomUUID();
      accepted = null;
    } else if (state.draft?.revision !== revision) {
      paused = true;
      showComparison(state);
    }
  }

  return { ensureSaved, observeHistory, blockEditor, assignment };
}
