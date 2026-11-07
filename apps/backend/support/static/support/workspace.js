import { requestJson } from "alliance/api";
import { attachDraftEditor } from "alliance/draft-editor";
import { startPolling } from "alliance/polling";

let editor, polling;
const beginMutation = () => polling?.beginMutation() ?? (() => {});
const closedForms = new WeakSet();

function bindEditor(form) {
  editor = attachDraftEditor(form, { beginMutation });
}

const editorForm = document.querySelector("[data-send-response]");
if (editorForm) bindEditor(editorForm);

const errors = {
  review_state_changed: "Сообщение или обращение изменилось. Обновите страницу.",
  assignment_version_conflict: "Назначение изменилось. Обновите страницу.",
  target_owner_unavailable: "Выбранный менеджер больше недоступен. Обнови список.",
  ticket_already_assigned: "Обращение уже взял другой менеджер. Обновите страницу.",
  reconciliation_in_progress: "Проверка уже идёт. Обновите страницу.",
  reconciliation_not_required: "Исход уже известен. Обновите страницу.",
  outcome_unknown: "Повтор заблокирован, пока исход отправки не выяснен.",
  operation_state_changed: "Обращение изменилось. Обновите страницу.",
};

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.matches("[data-support-command]")) return;
  event.preventDefault();
  if (form.querySelector("button[type=submit]").disabled) return;
  const button = form.querySelector("button[type=submit]");
  const errorBox = form.querySelector("[data-command-error]");
  const { csrfmiddlewaretoken, ...body } = Object.fromEntries(new FormData(form));
  if ("expected_ticket_id" in body) body.expected_ticket_id ||= null;
  if ("expected_assignment_version" in body) {
    body.expected_assignment_version = Number(body.expected_assignment_version);
  }
  button.disabled = true;
  errorBox.textContent = "";
  const release = beginMutation();
  let success = false;
  try {
    const result = await requestJson(form.action, { method: "POST", body, csrfToken: csrfmiddlewaretoken });
    success = true;
    button.blur();
    if (form.hasAttribute("data-attachment-prepare")) {
      form.hidden = true;
      const status = form.parentElement.querySelector("[data-attachment-status]");
      status.textContent = "Подготовка началась. Обновите страницу, чтобы проверить результат.";
    }
    if (form.hasAttribute("data-ticket-result")) {
      window.location.assign(`/support/tickets/${result.ticket.id}/`);
    } else if (form.dataset.returnUrl) {
      window.location.assign(form.dataset.returnUrl);
    }
  } catch (error) {
    const details = error.payload?.error;
    errorBox.textContent = errors[details?.code] || details?.message || "Не удалось выполнить действие. Попробуйте ещё раз.";
  } finally {
    release(success);
    button.disabled = success;
  }
});


function bindCloseForm(closeForm) {
  if (!closeForm || closedForms.has(closeForm)) return;
  closedForms.add(closeForm);
  const submit = closeForm.querySelector("button[type=submit]");
  const confirm = closeForm.querySelector("[data-confirm-close]");
  const warning = closeForm.querySelector("[data-close-warning]");
  const reasons = closeForm.querySelector("[data-close-reasons]");
  const errorBox = closeForm.querySelector("[data-command-error]");
  const csrfToken = closeForm.elements.csrfmiddlewaretoken.value;
  const version = Number(closeForm.elements.expected_assignment_version.value);
  let token = null;
  let command = null;

  function displayWarning(details) {
    token = details?.warning_token ?? null;
    warning.hidden = !token;
    reasons.replaceChildren();
    if (!token) return;
    const notes = [
      [details.has_failed_responses, "Есть ответы с подтверждённой ошибкой отправки."],
      [details.has_waiting_attempts, "Ответы в очереди будут отменены."],
      [details.has_started_attempts, "Начатые отправки могут завершиться после закрытия."],
      [details.has_unknown_outcome, "Некоторые сообщения могли уйти. Их исход пока неизвестен."],
      [details.has_uncommitted_draft, "Личный черновик останется в карточке."],
    ];
    for (const [show, text] of notes) {
      if (show) {
        const item = document.createElement("li");
        item.textContent = text;
        reasons.append(item);
      }
    }
  }

  async function closeTicket(confirmed) {
    submit.disabled = confirm.disabled = true;
    errorBox.textContent = "";
    const release = beginMutation();
    let success = false;
    try {
      if (editor && !await editor.ensureSaved()) {
        errorBox.textContent = "Сначала сохрани личный черновик.";
        return;
      }
      if (!command) {
        command = {
          client_operation_id: closeForm.elements.client_operation_id.value,
          expected_assignment_version: version,
          confirm_warning: confirmed,
          warning_token: confirmed ? token : null,
        };
      }
      await requestJson(closeForm.action, { method: "POST", body: command, csrfToken });
      success = true;
      document.activeElement?.blur();
      command = null;
      displayWarning(null);
    } catch (error) {
      if (error.status >= 400 && error.status < 500) {
        command = null;
        closeForm.elements.client_operation_id.value = crypto.randomUUID();
        displayWarning(error.payload?.warning);
        errorBox.textContent = error.payload?.error?.message || "Не удалось закрыть обращение.";
      } else {
        errorBox.textContent = "Подтверждение не пришло. Повтори действие, чтобы узнать его результат.";
      }
    } finally {
      release(success);
      submit.disabled = confirm.disabled = success;
    }
  }

  closeForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void closeTicket(false);
  });
  confirm.addEventListener("click", () => void closeTicket(true));
}



bindCloseForm(document.querySelector("[data-close-ticket]"));
const workspace = document.querySelector("[data-queue-url], [data-ticket-state-url]");
if (workspace) {
  polling = startPolling(workspace, {
    onState(state) {
      if (editor && (!state.permissions.can_edit_draft || editor.assignment !== state.ticket.assignment_version)) {
        editor.blockEditor();
      }
    },
    async onHeader(header, state, signal, read, current) {
      bindCloseForm(header.querySelector("[data-close-ticket]"));
      const template = header.querySelector("[data-editor-template]");
      if (!template || editor || header.dataset.stateKey !== state.state_key) return;
      const fragment = template.content.cloneNode(true);
      const form = fragment.querySelector("[data-send-response]");
      const draft = await read(form.dataset.draftUrl, "application/json", signal);
      if (!current() || !draft.editable || draft.assignment_version !== state.ticket.assignment_version) return;
      form.elements.text.value = draft.draft?.text ?? "";
      form.elements.draft_id.value = draft.draft?.id ?? "";
      form.elements.draft_revision.value = draft.draft_revision;
      workspace.querySelector("[data-editor-slot]").replaceChildren(fragment);
      bindEditor(form);
    },
    async onHistory(history, readDraft, current) {
      if (editor && current()) await editor.observeHistory(history, readDraft);
    },
    onStop() {
      editor?.blockEditor();
    },
  });
}
