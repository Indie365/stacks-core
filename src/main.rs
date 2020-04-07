/*
 copyright: (c) 2013-2019 by Blockstack PBC, a public benefit corporation.

 This file is part of Blockstack.

 Blockstack is free software. You may redistribute or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License or
 (at your option) any later version.

 Blockstack is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY, including without the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with Blockstack. If not, see <http://www.gnu.org/licenses/>.
*/

#![allow(unused_imports)]
#![allow(dead_code)]
#![allow(non_camel_case_types)]
#![allow(non_snake_case)]
#![allow(non_upper_case_globals)]

extern crate rand;
extern crate ini;
extern crate secp256k1;
extern crate serde;
extern crate rusqlite;
extern crate curve25519_dalek;
extern crate ed25519_dalek;
#[macro_use] extern crate lazy_static;
extern crate sha2;
extern crate sha3;
extern crate ripemd160;
extern crate regex;
extern crate time;
extern crate byteorder;
extern crate mio;
extern crate url;

#[macro_use] extern crate serde_derive;
#[macro_use] extern crate serde_json;

#[cfg(test)]
#[macro_use]
extern crate assert_json_diff;

#[macro_use]
extern crate blockstack_lib;
use blockstack_lib::*;

use std::fs;
use std::env;
use std::process;
use std::io::prelude::*;
use std::io;

use util::log;

use net::StacksMessageCodec;
use chainstate::stacks::*;
use util::hash::{hex_bytes, to_hex};
use util::retry::LogReader;

fn main() {

    log::set_loglevel(log::LOG_INFO).unwrap();

    let argv : Vec<String> = env::args().collect();
    if argv.len() < 2 {
        eprintln!("Usage: {} command [args...]", argv[0]);
        process::exit(1);
    }

    if argv[1] == "decode-bitcoin-header" {
        if argv.len() < 4 {
            eprintln!("Usage: {} decode-bitcoin-header BLOCK_HEIGHT PATH", argv[0]);
            process::exit(1);
        }

        use burnchains::bitcoin::spv;

        let height = argv[2].parse::<u64>().expect("Invalid block height");
        let headers_path = &argv[3];

        let header_opt = spv::SpvClient::read_block_header(headers_path, height).unwrap();
        match header_opt {
            Some(header) => {
                println!("{:#?}", header);
                process::exit(0);
            },
            None => {
                eprintln!("Failed to read header");
                process::exit(1);
            }
        }
    }

    if argv[1] == "decode-tx" {
        if argv.len() < 3 {
            eprintln!("Usage: {} decode-tx TRANSACTION", argv[0]);
            process::exit(1);
        }

        let tx_str = &argv[2];
        let tx_bytes = hex_bytes(tx_str).map_err(|_e| {
            eprintln!("Failed to decode transaction: must be a hex string");
            process::exit(1);
        }).unwrap();

        let mut cursor = io::Cursor::new(&tx_bytes);
        let mut debug_cursor = LogReader::from_reader(&mut cursor);

        let tx = StacksTransaction::consensus_deserialize(&mut debug_cursor).map_err(|e| {
            eprintln!("Failed to decode transaction: {:?}", &e);
            eprintln!("Bytes consumed:");
            for buf in debug_cursor.log().iter() {
                eprintln!("  {}", to_hex(buf));
            }
            process::exit(1);
        }).unwrap();

        println!("{:#?}", &tx);
        process::exit(0);
    }

    if argv[1] == "decode-block" {
        if argv.len() < 3 {
            eprintln!("Usage: {} decode-block BLOCK_PATH", argv[0]);
            process::exit(1);
        }

        let block_path = &argv[2];
        let block_data = fs::read(block_path).expect(&format!("Failed to open {}", block_path));

        let block = StacksBlock::consensus_deserialize(&mut io::Cursor::new(&block_data)).map_err(|_e| {
            eprintln!("Failed to decode block");
            process::exit(1);
        }).unwrap();

        println!("{:#?}", &block);
        process::exit(0);
    }

    if argv[1] == "decode-microblocks" {
        if argv.len() < 3 {
            eprintln!("Usage: {} decode-microblocks MICROBLOCK_STREAM_PATH", argv[0]);
            process::exit(1);
        }

        let mblock_path = &argv[2];
        let mblock_data = fs::read(mblock_path).expect(&format!("Failed to open {}", mblock_path));

        let mut cursor = io::Cursor::new(&mblock_data);
        let mut debug_cursor = LogReader::from_reader(&mut cursor);
        let mblocks : Vec<StacksMicroblock> = Vec::consensus_deserialize(&mut debug_cursor).map_err(|e| {
            eprintln!("Failed to decode microblocks: {:?}", &e);
            eprintln!("Bytes consumed:");
            for buf in debug_cursor.log().iter() {
                eprintln!("  {}", to_hex(buf));
            }
            process::exit(1);
        }).unwrap();

        println!("{:#?}", &mblocks);
        process::exit(0);
    }

    if argv[1] == "exec_program" {
        if argv.len() < 3 {
            eprintln!("Usage: {} exec_program [program-file.clar]", argv[0]);
            process::exit(1);
        }
        let program: String = fs::read_to_string(&argv[2])
            .expect(&format!("Error reading file: {}", argv[2]));
        match vm::execute(&program) {
            Ok(Some(result)) => println!("{}", result),
            Ok(None) => println!(""),
            Err(error) => { 
                panic!("Program Execution Error: \n{}", error);
            }
        }
        return
    }

    if argv[1] == "testnet" {
        use testnet;

        let conf = match argv.len() {
            n if n >= 3 => {
                println!("Starting testnet with config {}...", argv[2]);
                testnet::helium::Config::from_config_file_path(&argv[2])
            },
            _ => {
                println!("Starting testnet with default config...");
                testnet::helium::Config::default()
            }
        };

        println!("*** Mempool path: {}", conf.mempool.path);

        let mut run_loop = testnet::helium::RunLoop::new(conf);
        let num_round: u64 = 0; // Infinite number of rounds
        run_loop.start(num_round);
        return
    }

    if argv[1] == "docgen" {
        println!("{}", vm::docs::make_json_api_reference());
        return
    }

    if argv[1] == "local" {
        clarity::invoke_command(&format!("{} {}", argv[0], argv[1]), &argv[2..]);
        return
    }

    if argv.len() < 4 {
        eprintln!("Usage: {} blockchain network working_dir", argv[0]);
        process::exit(1);
    }

    let blockchain = &argv[1];
    let network = &argv[2];
    let working_dir = &argv[3];

    match (blockchain.as_str(), network.as_str()) {
        ("bitcoin", "mainnet") | ("bitcoin", "testnet") | ("bitcoin", "regtest") => {
            let block_height_res = core::sync_burnchain_bitcoin(&working_dir, &network);
            match block_height_res {
                Err(e) => {
                    eprintln!("Failed to sync {} {}: {:?}", blockchain, network, e);
                    process::exit(1);
                },
                Ok(height) => {
                    println!("Synchronized state to block {}", height);
                }
            }
        },
        (_, _) => {
            eprintln!("Unrecognized blockchain and/or network");
            process::exit(1);
        }
    };
}
