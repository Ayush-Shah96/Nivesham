import React from 'react';
import AdminUsage from './AdminUsage';
import UserSettings from './UserSettings';

// Settings opens the account page for everyone. Admins get the full workspace
// (User Settings, App Access, Usage & Sessions, Manage Users) instead.
export default function Settings({ user }) {
  if (user?.role === 'ADMIN') return <AdminUsage user={user} />;
  return <UserSettings user={user} />;
}
