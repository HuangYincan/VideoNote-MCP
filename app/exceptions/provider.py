# exceptions.py
class ProviderError(Exception):
    def __init__(self, message: str,code: int) -> None:
        super().__init__(message)
        self.code=code
        self.message = message
        
        

