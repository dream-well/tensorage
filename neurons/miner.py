# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 salahawk <tylermcguy@gmail.com>

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

# Storage Subnet Miner code:

# Step 1: Import necessary libraries and modules
import os
import time
import argparse
import traceback
import bittensor as bt

# Custom modules
import json
import torch
import typing
import allocate
import sqlite3
from tqdm import tqdm

# import this repo
import storage
import threading


def get_config():
    # Step 2: Set up the configuration parser
    # This function initializes the necessary command-line arguments.
    # Using command-line arguments allows users to customize various miner settings.
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db_root_path",
        default="~/bittensor-db",
        help="Miner generated by partitioning your data drive.",
    )
    # Adds override arguments for network and netuid.
    parser.add_argument("--netuid", type=int, default=1, help="The chain subnet uid.")
    # The number of steps between reallocations.
    parser.add_argument(
        "--steps_per_reallocate",
        type=int,
        default=1000,
        help="The number of steps between reallocations.",
    )
    # The proportion of available space used to store data.
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.001,
        required=False,
        help="Size of path to fill",
    )
    # If set, the miner will realocate its DB entirely (this is expensive and not recommended)
    parser.add_argument(
        "--restart", action="store_true", default=False, help="Restart the db."
    )
    # Adds subtensor specific arguments i.e. --subtensor.chain_endpoint ... --subtensor.network ...
    bt.subtensor.add_args(parser)
    # Adds logging specific arguments i.e. --logging.debug ..., --logging.trace .. or --logging.logging_dir ...
    bt.logging.add_args(parser)
    # Adds wallet specific arguments i.e. --wallet.name ..., --wallet.hotkey ./. or --wallet.path ...
    bt.wallet.add_args(parser)
    # Adds axon specific arguments i.e. --axon.port ...
    bt.axon.add_args(parser)
    # Activating the parser to read any command-line inputs.
    # To print help message, run python3 neurons/miner.py --help
    config = bt.config(parser)

    # Step 3: Set up logging directory
    # Logging captures events for diagnosis or understanding miner's behavior.
    config.full_path = os.path.expanduser(
        "{}/{}/{}/netuid{}/{}".format(
            config.logging.logging_dir,
            config.wallet.name,
            config.wallet.hotkey,
            config.netuid,
            "miner",
        )
    )
    # Ensure the directory for logging exists, else create one.
    if not os.path.exists(config.full_path):
        os.makedirs(config.full_path, exist_ok=True)
    return config


# Main takes the config and starts the miner.
def main(config):
    # Activating Bittensor's logging with the set configurations.
    config.db_root_path = os.path.expanduser(config.db_root_path)
    bt.logging(config=config, logging_dir=config.full_path)
    bt.logging.info(
        f"Running miner for subnet: {config.netuid} on network: {config.subtensor.chain_endpoint} with config:"
    )

    # This logs the active configuration to the specified logging directory for review.
    bt.logging.info(config)

    # Step 4: Initialize Bittensor miner objects
    # These classes are vital to interact and function within the Bittensor network.
    bt.logging.info("Setting up bittensor objects.")

    # Wallet holds cryptographic information, ensuring secure transactions and communication.
    wallet = bt.wallet(config=config)
    bt.logging.info(f"Wallet: {wallet}")

    # subtensor manages the blockchain connection, facilitating interaction with the Bittensor blockchain.
    subtensor = bt.subtensor(config=config)
    bt.logging.info(f"Subtensor: {subtensor}")

    # metagraph provides the network's current state, holding state about other participants in a subnet.
    metagraph = subtensor.metagraph(config.netuid)
    bt.logging.info(f"Metagraph: {metagraph}")

    if wallet.hotkey.ss58_address not in metagraph.hotkeys:
        bt.logging.error(
            f"\nYour miner: {wallet} is not registered to chain connection: {subtensor} \nRun btcli s register and try again. "
        )
        exit()
    else:
        # Each miner gets a unique identity (UID) in the network for differentiation.
        my_subnet_uid = metagraph.hotkeys.index(wallet.hotkey.ss58_address)
        bt.logging.info(f"Running miner on uid: {my_subnet_uid}")

    # Create DBs
    allocations = {
        a["validator"]: a
        for a in allocate.allocate(
            db_root_path=config.db_root_path,
            wallet=wallet,
            metagraph=metagraph,
            threshold=config.threshold,
            hash=False,
        )
    }
    bt.logging.info(
        f"Creating: {len(allocations)} with details: {json.dumps(allocations, indent=4, sort_keys=True)}"
    )
    # Generate the data allocations.
    allocate.generate(
        allocations=list(allocations.values()),  # The allocations to generate.
        no_prompt=True,  # If True, no prompt will be shown
        workers=10,  # The number of concurrent workers to use for generation. Default is 10.
        restart=config.restart,  # If true, the miner will realocate its DB entirely (this is expensive and not recommended)
    )

    # Connect to SQLite databases.
    local_storage = threading.local()

    def close_db_connections():
        # Iterate over all connections in local_storage and close them
        for attr in dir(local_storage):
            if attr.startswith("connection_"):
                connection = getattr(local_storage, attr)
                connection.close()
                bt.logging.info(f"Closed database connection: {attr}")

    def get_db_connection(alloc):
        # Check if we have a connection for this thread
        if not hasattr(local_storage, f"connection_{alloc['validator']}"):
            bt.logging.info(f"Connecting to database under path: {alloc['path']}")
            setattr(
                local_storage,
                f"connection_{alloc['validator']}",
                sqlite3.connect(alloc["path"]),
            )
        return getattr(local_storage, f"connection_{alloc['validator']}")

    async def retrieve(synapse: storage.protocol.Retrieve) -> storage.protocol.Retrieve:
        # Check if we have the data connection locally
        bt.logging.info(
            f"Got RETRIEVE request for key: {synapse.key} from dendrite: {synapse.dendrite.hotkey}"
        )  # Connect to SQLite databases

        db = get_db_connection(allocations[synapse.dendrite.hotkey])
        cursor = db.cursor()

        # Fetch data from SQLite databases
        query = f"SELECT data FROM DB{wallet.hotkey.ss58_address}{synapse.dendrite.hotkey} WHERE id=?"
        cursor.execute(query, (synapse.key,))
        data_value = cursor.fetchone()

        # Set data to None if key not found
        if data_value:
            synapse.data = data_value[0]
            bt.logging.success(f"Found data for key {synapse.key}!")
        else:
            synapse.data = None
            bt.logging.error(f"Data not found for key {synapse.key}!")
        return synapse

    async def store(synapse: storage.protocol.Store) -> storage.protocol.Store:
        # Check if we have the data connection locally
        bt.logging.info(
            f"Got STORE request for key: {synapse.key} from dendrite: {synapse.dendrite.hotkey}"
        )
        # Connect to SQLite databases
        db = get_db_connection(allocations[synapse.dendrite.hotkey])
        cursor = db.cursor()
        bt.logging.info(
            f"Got STORE request to store data: {synapse.data} under key: {synapse.key}"
        )

        # Insert data into SQLite databases
        try:
            update_request = f"UPDATE DB{wallet.hotkey.ss58_address}{synapse.dendrite.hotkey} SET data = ? WHERE id = ?"
            cursor.execute(update_request, (synapse.data, synapse.key))
            db.commit()
        except Exception as e:
            bt.logging.error(f"Error updating database: {e}")

        # Return
        bt.logging.success(f"Stored data for key {synapse.key}!")
        return synapse

    # Step 5: Build and link miner functions to the axon.
    # The axon handles request processing, allowing validators to send this process requests.
    axon = bt.axon(wallet=wallet)
    bt.logging.info(f"Axon {axon}")

    # Attach determiners which functions are called when servicing a request.
    bt.logging.info(f"Attaching forward function to axon.")
    axon.attach(retrieve).attach(store)

    # Serve passes the axon information to the network + netuid we are hosting on.
    # This will auto-update if the axon port of external ip have changed.
    bt.logging.info(
        f"Serving axon {store} and {retrieve} on network: {config.subtensor.chain_endpoint} with netuid: {config.netuid}"
    )
    axon.serve(netuid=config.netuid, subtensor=subtensor)

    # Start  starts the miner's axon, making it active on the network.
    bt.logging.info(f"Starting axon server on port: {config.axon.port}")
    axon.start()

    # Step 6: Keep the miner alive
    # This loop maintains the miner's operations until intentionally stopped.
    bt.logging.info(f"Starting main loop")
    step = 0
    while True:
        try:
            # Below: Periodically update our knowledge of the network graph.
            if step % 5 == 0:
                metagraph = subtensor.metagraph(config.netuid)
                log = (
                    f"Step:{step} | "
                    f"Block:{metagraph.block.item()} | "
                    f"Stake:{metagraph.S[my_subnet_uid]} | "
                    f"Rank:{metagraph.R[my_subnet_uid]} | "
                    f"Trust:{metagraph.T[my_subnet_uid]} | "
                    f"Consensus:{metagraph.C[my_subnet_uid] } | "
                    f"Incentive:{metagraph.I[my_subnet_uid]} | "
                    f"Emission:{metagraph.E[my_subnet_uid]}"
                )
                bt.logging.info(log)

            if step % config.steps_per_reallocate == 0:
                metagraph = subtensor.metagraph(config.netuid)
                allocations = {
                    a["validator"]: a
                    for a in allocate.allocate(
                        db_root_path=config.db_root_path,
                        wallet=wallet,
                        metagraph=metagraph,
                        threshold=config.threshold,
                        hash=False,
                    )
                }
                bt.logging.info(
                    f"Reallocating: {len(allocations)} with details: {json.dumps(allocations, indent=4, sort_keys=True)}"
                )
                # Generate the data allocations.
                allocate.generate(
                    allocations=list(
                        allocations.values()
                    ),  # The allocations to generate.
                    no_prompt=True,  # If True, no prompt will be shown
                    workers=10,  # The number of concurrent workers to use for generation. Default is 10.
                    restart=False,  # If true, the miner will realocate its DB entirely (this is expensive and not recommended)
                )

            step += 1
            time.sleep(1)

        # If someone intentionally stops the miner, it'll safely terminate operations.
        except KeyboardInterrupt:
            axon.stop()
            close_db_connections()  # Close all db connections
            bt.logging.success("Miner killed by keyboard interrupt.")
            break
        # In case of unforeseen errors, the miner will log the error and continue operations.
        except Exception as e:
            bt.logging.error(traceback.format_exc())
            continue


# This is the main function, which runs the miner.
if __name__ == "__main__":
    main(get_config())