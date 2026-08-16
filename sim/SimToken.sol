// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title SimToken
/// @notice Minimal ERC-20 used only by the simulation stack.
///
/// Deliberately deployed as a real contract on a real (local) chain rather than
/// mocked in Python: the gateway then exercises its actual signing, nonce
/// allocation, gas estimation, receipt and confirmation logic. A mock would
/// skip precisely the code that is hardest to get right.
///
/// Six decimals, matching USDT, so the fiat-to-units conversion under test is
/// the same arithmetic production will run.
contract SimToken {
    string public constant name = "Simulation USD";
    string public constant symbol = "sUSD";
    uint8 public constant decimals = 6;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(uint256 initialSupply) {
        totalSupply = initialSupply;
        balanceOf[msg.sender] = initialSupply;
        emit Transfer(address(0), msg.sender, initialSupply);
    }

    function transfer(address to, uint256 value) external returns (bool) {
        require(to != address(0), "SimToken: transfer to the zero address");
        require(balanceOf[msg.sender] >= value, "SimToken: insufficient balance");
        balanceOf[msg.sender] -= value;
        balanceOf[to] += value;
        emit Transfer(msg.sender, to, value);
        return true;
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        require(balanceOf[from] >= value, "SimToken: insufficient balance");
        require(allowance[from][msg.sender] >= value, "SimToken: insufficient allowance");
        allowance[from][msg.sender] -= value;
        balanceOf[from] -= value;
        balanceOf[to] += value;
        emit Transfer(from, to, value);
        return true;
    }

    /// @notice Open faucet. This is a simulation asset with no value.
    function mint(address to, uint256 value) external {
        totalSupply += value;
        balanceOf[to] += value;
        emit Transfer(address(0), to, value);
    }
}
