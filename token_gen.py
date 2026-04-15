import uuid
import time

def generate_dynamic_token():
    # This creates a random unique ID
    token = str(uuid.uuid4())
    print(f"New Token Generated: {token}")
    return token

# Simulate the 10-20 second refresh mentioned in your synopsis
print("Generating tokens every 10 seconds...")  
for i in range(3):
    generate_dynamic_token()
    time.sleep(10)