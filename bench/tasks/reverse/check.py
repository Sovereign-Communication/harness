from reverse import reverse_str

assert reverse_str("hello") == "olleh"
assert reverse_str("") == ""
assert reverse_str("a") == "a"
assert reverse_str("racecar") == "racecar"
print("reverse: ok")