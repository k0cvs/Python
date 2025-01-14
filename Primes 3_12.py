import time
t0=time.time()

prime_numbers =[]
for x in range(2,101):
    for primes in prime_numbers:
        if x % primes == 0:
            break
    else:
        prime_numbers.append(x)
            
print(prime_numbers) 
    
print("\n")
t1=time.time()
print("Time required: ", t1 - t0, " seconds.")


            
# end=" "      
    
    
