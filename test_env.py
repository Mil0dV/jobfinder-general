import os
from dotenv import load_dotenv

# Create a dummy .env file with a long key
long_key = "sk-svcacct-" + "a" * 150
with open(".env.test", "w") as f:
    f.write(f"TEST_KEY={long_key}\n")

load_dotenv(".env.test")

loaded_key = os.getenv("TEST_KEY")
print(f"Original length: {len(long_key)}")
print(f"Loaded length: {len(loaded_key) if loaded_key else 0}")
print(f"Match: {long_key == loaded_key}")
