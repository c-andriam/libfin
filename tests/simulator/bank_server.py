import asyncio
import logging
from libfin.network.server import Iso8583Server
from libfin import iso8583

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

async def handle_message(msg_bytes: bytes) -> bytes:
    try:
        msg_dict = iso8583.loads(msg_bytes)
        LOGGER.info(f"Bank Server received: MTI={msg_dict.get('MTI')}, STAN={msg_dict.get('DE11')}, Amount={msg_dict.get('DE4')}")
        
        # Approve all 0200 requests
        if msg_dict.get("MTI") == "0200":
            # Response MTI
            msg_dict["MTI"] = "0210"
            # Set Action Code to "00" (Approved)
            msg_dict["DE39"] = "00"
            
            LOGGER.info(f"Bank Server approving request. STAN={msg_dict.get('DE11')}")
            return iso8583.dumps(msg_dict)
            
    except Exception as e:
        LOGGER.error(f"Error handling message: {e}")
        
    return b""

async def main():
    server = Iso8583Server("0.0.0.0", 9000, handle_message, length_header_size=2)
    LOGGER.info("Starting Mock Bank Server on 0.0.0.0:9000")
    await server.start()
    
    # Keep server running
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
