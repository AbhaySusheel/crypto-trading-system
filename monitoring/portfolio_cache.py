import time

class PortfolioCache:
    def __init__(self, refresh_sec=10):
        self.refresh_sec = refresh_sec
        self.last_update = 0
        self.data = None

    def should_refresh(self):
        return time.time() - self.last_update > self.refresh_sec

    def update(self, fetch_func):
        self.data = fetch_func()
        self.last_update = time.time()
        return self.data

    def get(self):
        return self.data