import { request } from './api';

const send = (path, method, data) =>
  request(path, {
    method,
    body: data === undefined ? undefined : JSON.stringify(data),
  });

export const opsTrackerApi = {
  me: () => request('/ops-tracker/me', { retry: true }),
  board: () => request('/ops-tracker/board', { retry: true }),
  loginUsers: () => request('/ops-tracker/login-users', { retry: true }),
  linkMember: (id, userId) => send(`/ops-tracker/members/${id}/link`, 'POST', { user_id: userId }),
  autoLinkMembers: () => send('/ops-tracker/members/auto-link', 'POST', {}),
  createTeam: (data) => send('/ops-tracker/teams', 'POST', data),
  updateTeam: (id, data) => send(`/ops-tracker/teams/${id}`, 'PATCH', data),
  deleteTeam: (id) => send(`/ops-tracker/teams/${id}`, 'DELETE'),
  createMember: (data) => send('/ops-tracker/members', 'POST', data),
  updateMember: (id, data) => send(`/ops-tracker/members/${id}`, 'PATCH', data),
  deleteMember: (id) => send(`/ops-tracker/members/${id}`, 'DELETE'),
  createTask: (data) => send('/ops-tracker/tasks', 'POST', data),
  updateTask: (id, data) => send(`/ops-tracker/tasks/${id}`, 'PATCH', data),
  deleteTask: (id) => send(`/ops-tracker/tasks/${id}`, 'DELETE'),
};
