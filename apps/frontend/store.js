// Only synthetic demo data. This is not authentication or a server database.
const KEY = 'alliance-support-demo-v1';
export function seed() {
  return {
    version: 1,
    people: [
      { id: 'manager', name: 'Михаил Ковалёв', role: 'manager', status: 'active' },
      { id: 'admin', name: 'Ольга Белова', role: 'admin', status: 'active' },
      { id: 'applicant', name: 'Анна Лебедева', role: null, status: 'pending' }
    ],
    tickets: [
      { id: '1042', name: 'Артём Смирнов', topic: 'Расписание секции', tag: 'Баскетбол', owner: null, status: 'open', important: true, reason: 'Студент попросил сотрудника', messages: [{ by: 'student', text: 'Подскажите, где уточнить время ближайшего занятия?' }, { by: 'bot', text: 'Передаю ваш вопрос менеджерам клуба. Ответ может занять время.' }] },
      { id: '1041', name: 'Елена Романова', topic: 'Первое занятие', tag: 'Волейбол', owner: 'admin', status: 'open', important: false, reason: 'Нет подходящих сведений', messages: [{ by: 'student', text: 'Что нужно взять на первое занятие?' }] },
      { id: '1039', name: 'Даниил Орлов', topic: 'Контакт организатора', tag: 'Общий вопрос', owner: 'manager', status: 'closed', important: false, reason: 'Просьба о человеке', messages: [{ by: 'student', text: 'Спасибо, всё понятно.' }] }
    ],
    docs: [{ id: 'intro', draft: null, published: { title: 'Как пользоваться демонстрацией', text: 'Это демонстрационный материал, а не регламент клуба. Время занятий уточняет менеджер секции. Реальные документы клуба пока не загружены.', version: 1 }, active: true }]
  };
}
export function load() {
  try {
    const state = JSON.parse(localStorage.getItem(KEY));
    if (state?.version === 1 && Array.isArray(state.people) && Array.isArray(state.tickets) && Array.isArray(state.docs)) return state;
  } catch { /* Invalid or unavailable browser storage: start with fixtures. */ }
  return seed();
}
export function save(state) { localStorage.setItem(KEY, JSON.stringify(state)); }
export function reset() { localStorage.removeItem(KEY); return seed(); }
export function canWork(person) { return person?.status === 'active'; }
export function canReply(person, ticket) { return canWork(person) && ticket?.owner === person.id && ticket.status !== 'closed'; }
export function searchSources(docs, query) {
  const terms = query.toLocaleLowerCase('ru').split(/\s+/).filter(Boolean);
  if (!terms.length) return [];
  return docs.filter(d => d.active && d.published).map(d => ({ ...d.published, id: d.id }))
    .filter(d => terms.some(t => (d.title + ' ' + d.text).toLocaleLowerCase('ru').includes(t)));
}
