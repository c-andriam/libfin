from typing import Dict, Any


class TransactionContext:
    """
    Context for a single transaction.
    Wraps the parsed ISO8583 message and provides helper methods
    for building a response.
    """
    def __init__(self, message_dict: Dict[str, Any]):
        self.request = message_dict
        self.response = None
        self.mti = message_dict.get('MTI', '')

    def create_response(self) -> Dict[str, Any]:
        """
        Initializes a response dictionary based on the request.
        For example, converts MTI 0200 to 0210.
        Copies essential fields like STAN (DE11) and RRN (DE37).
        """
        self.response = {}
        if self.mti.isdigit():
            # Standard MTI response modification (add 10, e.g. 0200 -> 0210)
            req_mti = int(self.mti)
            self.response['MTI'] = f"{req_mti + 10:04d}"

        # Copy trace and terminal fields if present
        for field in ['DE11', 'DE37', 'DE41']:
            if field in self.request:
                self.response[field] = self.request[field]

        return self.response

    def set_response_code(self, code: str):
        """
        Sets the response code (DE39).
        """
        if self.response is None:
            self.create_response()
        self.response['DE39'] = code
