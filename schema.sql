create table if not exists users (
  id bigint primary key,
  username text,
  first_name text,
  phone text,
  phone_verified boolean default false,
  balance numeric(18,6) default 0,
  referred_by bigint,
  referral_rewarded boolean default false,
  created_at timestamptz default now()
);

create table if not exists withdrawals (
  id bigint generated always as identity primary key,
  user_id bigint not null,
  amount numeric(18,6) not null,
  wallet text not null,
  status text default 'pending',
  admin_id bigint,
  created_at timestamptz default now(),
  processed_at timestamptz
);

create table if not exists required_channels (
  id bigint generated always as identity primary key,
  chat_id text unique not null,
  title text,
  created_at timestamptz default now()
);

insert into required_channels(chat_id,title)
values
('@BET_1XZX','BET_1XZX'),
('@GAP_BAZIN1','GAP_BAZIN1')
on conflict (chat_id) do nothing;
