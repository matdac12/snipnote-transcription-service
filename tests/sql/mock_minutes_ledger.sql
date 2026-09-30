-- STAND-IN for the real (unknown) minutes ledger, matching only what MinutesManager.swift proves:
--   get_user_minutes_balance() -> integer, debit_minutes(p_amount, p_meeting_id) -> new balance,
--   auth.uid()-based, a repeated debit for one meeting fails with a unique violation.
-- __MEETING_TYPE__ is text or uuid (the real parameter type is unknown; the migration must cope with both).
CREATE TABLE public.mock_minutes_balance (user_id uuid PRIMARY KEY, balance integer NOT NULL CHECK (balance >= 0));
CREATE TABLE public.mock_minutes_ledger (id bigserial PRIMARY KEY, user_id uuid NOT NULL, meeting_id __MEETING_TYPE__, amount integer NOT NULL,
                                         CONSTRAINT mock_ledger_unique UNIQUE (user_id, meeting_id));
ALTER TABLE public.mock_minutes_balance ENABLE ROW LEVEL SECURITY;   -- like the real thing: users only see their own row
CREATE POLICY own_balance ON public.mock_minutes_balance FOR SELECT TO authenticated USING (user_id = auth.uid());

CREATE FUNCTION public.get_user_minutes_balance() RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT COALESCE((SELECT balance FROM public.mock_minutes_balance WHERE user_id = auth.uid()), 0)
$$;

CREATE FUNCTION public.debit_minutes(p_amount integer, p_meeting_id __MEETING_TYPE__ DEFAULT NULL) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_new integer;
BEGIN
  IF auth.uid() IS NULL THEN RAISE EXCEPTION 'not authenticated'; END IF;
  INSERT INTO public.mock_minutes_ledger (user_id, meeting_id, amount) VALUES (auth.uid(), p_meeting_id, p_amount);
  UPDATE public.mock_minutes_balance SET balance = balance - p_amount WHERE user_id = auth.uid() RETURNING balance INTO v_new;
  RETURN v_new;   -- a balance < 0 violates the CHECK constraint, like "constraint" errors in the app's duplicate heuristics
END $$;
