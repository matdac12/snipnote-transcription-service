#!/usr/bin/env python3
"""
Cleanup script to delete all storage files for a specific user
Keeps statistics (user_usage, subscriptions) intact
"""

import os
from supabase_client import supabase

# User to clean up
USER_ID = "e6658fad-05e2-4d25-9779-857bc72bbc81"
USER_EMAIL = "mattia.dacampo@gmail.com"

def cleanup_user_storage():
    """Delete all storage files and database records for user, keep statistics"""

    print(f"🧹 Starting cleanup for user: {USER_EMAIL}")
    print(f"   User ID: {USER_ID}")
    print()

    # Step 1: Get all files in user's storage folder
    print("📂 Step 1: Listing files in storage bucket...")
    try:
        files = supabase.storage.from_("recordings").list(USER_ID)
        print(f"   Found {len(files)} files in storage")

        # Delete each file
        if files:
            print(f"🗑️  Deleting {len(files)} files from storage...")
            file_paths = [f"{USER_ID}/{file['name']}" for file in files]

            # Delete in batches of 100 (Supabase limit)
            for i in range(0, len(file_paths), 100):
                batch = file_paths[i:i+100]
                result = supabase.storage.from_("recordings").remove(batch)
                print(f"   Deleted batch {i//100 + 1} ({len(batch)} files)")

            print(f"✅ Deleted all {len(files)} files from storage")
        else:
            print("   No files found in storage")
    except Exception as e:
        print(f"⚠️  Error listing/deleting storage files: {e}")

    print()

    # Step 2: Delete audio_chunks records
    print("📋 Step 2: Deleting audio_chunks records...")
    try:
        # First get count
        count_result = supabase.table("audio_chunks").select("id", count="exact").eq("user_id", USER_ID).execute()
        count = count_result.count if hasattr(count_result, 'count') else len(count_result.data)

        if count > 0:
            delete_result = supabase.table("audio_chunks").delete().eq("user_id", USER_ID).execute()
            print(f"✅ Deleted {count} audio_chunks records")
        else:
            print("   No audio_chunks records found")
    except Exception as e:
        print(f"⚠️  Error deleting audio_chunks: {e}")

    print()

    # Step 3: Delete transcription_jobs records
    print("🔨 Step 3: Deleting transcription_jobs records...")
    try:
        # First get count
        count_result = supabase.table("transcription_jobs").select("id", count="exact").eq("user_id", USER_ID).execute()
        count = count_result.count if hasattr(count_result, 'count') else len(count_result.data)

        if count > 0:
            delete_result = supabase.table("transcription_jobs").delete().eq("user_id", USER_ID).execute()
            print(f"✅ Deleted {count} transcription_jobs records")
        else:
            print("   No transcription_jobs records found")
    except Exception as e:
        print(f"⚠️  Error deleting transcription_jobs: {e}")

    print()

    # Step 4: Delete meetings records
    print("📅 Step 4: Deleting meetings records...")
    try:
        # First get count
        count_result = supabase.table("meetings").select("id", count="exact").eq("user_id", USER_ID).execute()
        count = count_result.count if hasattr(count_result, 'count') else len(count_result.data)

        if count > 0:
            delete_result = supabase.table("meetings").delete().eq("user_id", USER_ID).execute()
            print(f"✅ Deleted {count} meetings records")
        else:
            print("   No meetings records found")
    except Exception as e:
        print(f"⚠️  Error deleting meetings: {e}")

    print()

    # Step 5: Show preserved statistics
    print("📊 Step 5: Statistics preserved (not deleted):")
    try:
        usage = supabase.table("user_usage").select("*").eq("user_id", USER_ID).execute()
        if usage.data:
            user_data = usage.data[0]
            print(f"   ✅ user_usage: {user_data.get('total_meetings', 0)} meetings, {user_data.get('total_meeting_seconds', 0)} seconds tracked")

        subs = supabase.table("subscriptions").select("*").eq("user_id", USER_ID).execute()
        if subs.data:
            print(f"   ✅ subscriptions: {len(subs.data)} subscription record(s) preserved")
    except Exception as e:
        print(f"   ⚠️  Could not retrieve statistics: {e}")

    print()
    print("🎉 Cleanup complete!")
    print()
    print("Note: Statistics (user_usage, subscriptions) have been preserved.")
    print("You can now delete the meetings from your iPhone app.")


if __name__ == "__main__":
    cleanup_user_storage()
