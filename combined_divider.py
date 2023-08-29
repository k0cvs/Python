while(1):
    print("Choose your calculation:")
    print("1: Two resistor voltage divider")
    print("2: Three or more resistor divider")
    print("3. Exit")
    ask = input("> ")
    if (ask == "1"):
        print()
        print("--Calculating two resistor divider--")
        one = float(input("Enter resistotr one value: "))
        two = float(input("Enter resistor two value: "))
        volt = float(input("Enter input voltage: "))
        result = volt * two/(one + two)
        print()
        print("output voltage is ", result, "volts")
        print()
        print()

    elif(ask =="2"):
        print()
        print("--Calculating multi resistor divider--")
        first = float(input("Enter value of resistor of interest: "))
        total = float(input("Enter total resistance of divider network: "))
        volts = float(input("Enter input voltage: "))
        out = volts * first/total
        print()
        print("Output across resistor of interest is ", out, "volts")
        print()
        print()

    elif(ask == "3"):
        break

    else:
        print()
        print("Not an option, try again.")
        print()