import type { User } from './api';

export function Avatar({ user, small = false }: { user: Pick<User, 'name' | 'avatar'>; small?: boolean }) {
  return <span className={`avatar${small ? ' small' : ''}`}>{user.avatar ? <img src={user.avatar} alt=""/> : Array.from(user.name)[0]}</span>;
}
