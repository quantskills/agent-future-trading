"""
Supabase: Open source Postgres database as a service.
https://supabase.com/

Initialize the Supabase database tables and indices.
Note: The SQL commands below should be executed in the Supabase Table Editor to set up the database schema.
"""

-- Config table
create table if not exists config (
    id uuid primary key default uuid_generate_v4(),
    exp_name varchar(100) not null,
    updated_at timestamp with time zone default now(),
    tickers jsonb not null,
    has_planner boolean not null default false,
    llm_model varchar(50) not null,
    llm_provider varchar(50) not null
);

-- Portfolio table
create table if not exists portfolio (
    id uuid primary key default uuid_generate_v4(),
    config_id uuid references config(id),
    updated_at timestamp with time zone default now(),
    trading_date timestamp with time zone not null,
    cashflow decimal(15,2) not null,
    total_assets decimal(15,2) not null,
    positions jsonb not null
);

-- Signal table
create table if not exists signal (
    id uuid primary key default uuid_generate_v4(),
    portfolio_id uuid references portfolio(id),
    updated_at timestamp with time zone default now(),
    ticker varchar(10) not null,
    llm_prompt text not null,
    analyst varchar(50) not null,
    signal varchar(10) not null,
    justification text not null
);

-- Create indices
create index if not exists idx_config_exp_name on config(exp_name);
create index if not exists idx_portfolio_updated on portfolio(updated_at);
create index if not exists idx_portfolio_trading_date on portfolio(trading_date);
create index if not exists idx_signal_portfolio on signal(portfolio_id);
create index if not exists idx_signal_updated on signal(updated_at);
create index if not exists idx_signal_analyst on signal(analyst);
