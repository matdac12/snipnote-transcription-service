-- Disposable database only; minimal current production contract, no customer data.
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN CREATE ROLE anon; END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN CREATE ROLE authenticated; END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN CREATE ROLE service_role BYPASSRLS; END IF;
END $$;
CREATE SCHEMA auth; CREATE TABLE auth.users(id uuid PRIMARY KEY);
CREATE TYPE public.transcription_job_status AS ENUM ('pending','processing','completed','failed');
CREATE TABLE public.meetings(id uuid PRIMARY KEY,user_id uuid NOT NULL REFERENCES auth.users(id), name text NOT NULL,transcription_job_id uuid,has_recording boolean NOT NULL DEFAULT false,is_processing boolean NOT NULL DEFAULT false,processing_state text NOT NULL DEFAULT 'pending',total_chunks int DEFAULT 0,upload_status text DEFAULT 'completed',upload_progress int, uploaded_chunks int DEFAULT 0);
CREATE TABLE public.transcription_jobs(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid NOT NULL REFERENCES auth.users(id),meeting_id uuid NOT NULL,audio_url text,status public.transcription_job_status NOT NULL DEFAULT 'pending',duration float8,language varchar,transcription_provider text NOT NULL DEFAULT 'openai',is_chunked boolean DEFAULT false,total_chunks int DEFAULT 1,chunks_processed int DEFAULT 0);
CREATE TABLE public.audio_chunks(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),meeting_id uuid NOT NULL,user_id uuid NOT NULL,chunk_index int NOT NULL,total_chunks int NOT NULL,file_path text NOT NULL,file_size int NOT NULL,duration_seconds numeric NOT NULL, UNIQUE(meeting_id,chunk_index));
CREATE TABLE public.recordings(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid NOT NULL,meeting_id uuid NOT NULL,file_path text NOT NULL,duration int NOT NULL,file_size bigint NOT NULL);
GRANT USAGE ON SCHEMA public,auth TO service_role,anon,authenticated;
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
