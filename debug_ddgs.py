from ddgs import DDGS
from concurrent.futures import ThreadPoolExecutor, as_completed
import time


def search(q):
    print(f"Searching for: {q}")
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(q, max_results=5))
            print(f"Found {len(results)} results for {q}")
            return results
    except Exception as e:
        print(f"Error for {q}: {e}")
        return []


queries = [
    "Software Engineer jobs Switzerland",
    "Product Manager jobs Germany",
    "Data Scientist jobs Austria",
    "Climate Tech jobs France",
]

start = time.time()
all_res = []
with ThreadPoolExecutor(max_workers=4) as executor:
    futures = {executor.submit(search, q): q for q in queries}
    for future in as_completed(futures):
        all_res.extend(future.result())

print(f"Total results: {len(all_res)}")
print(f"Time taken: {time.time() - start:.2f}s")
