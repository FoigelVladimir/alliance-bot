import { requestJson } from "alliance/api";

// The frozen pending intention survives a lost response. Reads never replace editor input.
export function createReviewSession({ revision, path, csrfToken, request = requestJson,
                                      uuid = () => crypto.randomUUID(), changed = () => {} }) {
  const state = { revision, pending: null, busy: false, blocked: false,
                  comparison: null, needsComparison: false, message: "" };
  const compare = (payload) => {
    state.comparison = payload.review;
    state.comparisonRevision = payload.review?.revision ?? payload.review_revision;
    state.needsComparison = state.comparisonRevision !== state.revision;
  };
  function failure(error, mutating) {
    if (error.status === 409 && error.payload?.error?.code === "review_revision_conflict") {
      state.pending = null;
      compare(error.payload);
      state.message = "Оценка изменилась. Сравните актуальную оценку со своими правками.";
    } else {
      if (error.status && error.status < 500 && mutating) state.pending = null;
      if ([401, 403, 404].includes(error.status)) state.blocked = true;
      state.message = error.status && error.status < 500
        ? (error.payload?.error?.message || "Действие недоступно. Обновите страницу.")
        : (mutating ? "Ответ не получен. Повторите сохранение с теми же данными."
                    : "Не удалось прочитать оценку. Повторите проверку.");
    }
  }
  return {
    state,
    async save(problemFlag, comment) {
      if (state.busy || state.blocked || state.needsComparison) return;
      if (!state.pending) {
        if (Array.from(comment).length > 2000) {
          state.message = "Комментарий может содержать не более 2000 символов.";
          changed(state);
          return;
        }
        state.pending = Object.freeze({ client_operation_id: uuid(),
          expected_review_revision: state.revision, problem_flag: problemFlag, comment });
      }
      state.busy = true;
      changed(state);
      try {
        const result = await request(path, { method: "POST", body: state.pending, csrfToken });
        const applied = result?.operation?.applied_revision, current = result?.review?.revision;
        if (typeof applied !== "string" || !/^[1-9][0-9]*$/.test(applied)
            || typeof current !== "string" || !/^[1-9][0-9]*$/.test(current)) {
          throw new TypeError("The save response does not confirm an applied revision");
        }
        state.pending = null;
        // A replay may report an old applied revision alongside a colleague's newer review.
        if (result.operation.applied_revision === result.review.revision) {
          state.revision = result.review.revision;
          compare(result);
          state.message = "Оценка сохранена. История доступна после обновления страницы.";
        } else {
          compare(result);
          state.message = "Сохранение подтверждено, затем оценку изменил коллега. Сравните правки.";
        }
      } catch (error) {
        failure(error, true);
      } finally {
        state.busy = false;
        changed(state);
      }
    },
    async refresh() {
      if (state.busy || state.pending || state.blocked) return;
      state.busy = true;
      changed(state);
      try {
        const result = await request(path);
        compare(result);
        if (!result.can_edit) state.blocked = true;
        state.message = state.needsComparison
          ? "Оценка изменилась. Сравните её со своими правками."
          : "Показана актуальная оценка. Ваш комментарий сохранён в редакторе.";
      } catch (error) {
        failure(error, false);
      } finally {
        state.busy = false;
        changed(state);
      }
    },
    acceptRevision() {
      if (!state.needsComparison || state.busy || state.blocked) return;
      state.revision = state.comparisonRevision;
      state.needsComparison = false;
      state.message = "Актуальная ревизия принята. Проверьте свои правки и сохраните их.";
      changed(state);
    },
  };
}

export function attachReviewEditor(form) {
  const flag = form.elements.problem_flag, comment = form.elements.comment;
  const initialComment = form.querySelector("#ai-review-initial-comment");
  // HTML parsing normalizes CRLF and strips a leading textarea newline. Restore exact stored text.
  if (initialComment) comment.value = JSON.parse(initialComment.textContent);
  const submit = form.querySelector("[data-review-save]");
  const refresh = form.querySelector("[data-review-refresh]");
  const comparison = form.querySelector("[data-review-comparison]");
  const accept = form.querySelector("[data-review-accept]");
  const session = createReviewSession({
    revision: form.dataset.revision, path: form.action, csrfToken: form.elements.csrfmiddlewaretoken.value,
    changed(state) {
      flag.disabled = comment.readOnly = state.busy || Boolean(state.pending) || state.blocked;
      submit.disabled = state.busy || state.blocked || state.needsComparison;
      submit.textContent = state.pending ? "Повторить сохранение" : "Сохранить оценку";
      refresh.disabled = state.busy || Boolean(state.pending) || state.blocked;
      form.querySelector("[data-review-status]").textContent = state.message;
      comparison.hidden = state.comparisonRevision === undefined;
      const current = state.comparison;
      comparison.querySelector("[data-review-server-status]").textContent = current
        ? `${current.problem_flag ? "Есть проблема" : "Пометка снята"} · ревизия ${current.revision} · сотрудник ${current.updated_by_id} · ${current.updated_at}`
        : "Оценка ещё не сохранена.";
      comparison.querySelector("textarea").value = current?.comment ?? "";
      accept.hidden = !state.needsComparison;
      accept.disabled = state.busy || state.blocked;
    },
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    session.save(flag.checked, comment.value);
  });
  refresh.addEventListener("click", () => session.refresh());
  accept.addEventListener("click", () => session.acceptRevision());
  return session;
}

if (typeof document !== "undefined") {
  document.querySelectorAll("[data-ai-review]").forEach(attachReviewEditor);
}
