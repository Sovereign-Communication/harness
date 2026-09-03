from fizz import fizzbuzz

expected = ["1", "2", "Fizz", "4", "Buzz", "Fizz", "7", "8", "Fizz", "Buzz",
            "11", "Fizz", "13", "14", "FizzBuzz"]
assert fizzbuzz(15) == expected, fizzbuzz(15)
print("fizzbuzz: ok")